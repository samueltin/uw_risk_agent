"""
Underwriting Risk Assessment — Microsoft Agent Framework orchestrator
---------------------------------------------------------------------
The agent loop runs in THIS process. MCPStreamableHTTPTool is a
client-side connector, so this process dials the MCP server directly and
a localhost URL works with no tunnel and no public exposure.

The LLM behind the loop is chosen by UW_LLM_PROVIDER in .env:

    azure    Azure AI Foundry  — gpt-4.1 via FoundryChatClient
    ollama   Ollama            — e.g. llama3.1:8b on OLLAMA_HOST

Azure uses the Microsoft Agent Framework loop. Ollama uses deterministic
MCP evidence collection plus a JSON-mode final decision prompt because
smaller local models are less reliable at structured tool-call loops.

Served over HTTP by api/main.py.
Smoke test standalone with:  python -m api.orchestrator
"""

import os
import json
import time
import asyncio
import logging
import random
from pathlib import Path
from datetime import datetime, timezone
from contextlib import AsyncExitStack

from agent_framework import Agent, MCPStreamableHTTPTool

from models.submission import UnderwritingSubmission
from models.decision import UnderwritingDecision, Decision
from monitor.telemetry import track_llm_call, tracer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LLM provider selection — the only thing UW_LLM_PROVIDER changes
# ---------------------------------------------------------------------------

LLM_PROVIDER = os.environ.get("UW_LLM_PROVIDER", "azure").strip().lower()
MOCK_DECISION = os.environ.get("MOCK_DECISION", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
SUPPORTED_PROVIDERS = ("azure", "ollama")
OLLAMA_MAX_CONTEXT = int(os.environ.get("OLLAMA_NUM_CTX", "8192"))
OLLAMA_NUM_PREDICT = int(os.environ.get("OLLAMA_NUM_PREDICT", "1200"))
GUIDELINES_PATH = Path(__file__).resolve().parent.parent / "knowledge_base" / "uw_guidelines.md"
DECISION_QUEUE_NAMES = {
    Decision.ACCEPT: os.environ.get("UW_ACCEPT_QUEUE", "uw-decisions-accept"),
    Decision.REFER: os.environ.get("UW_REFER_QUEUE", "uw-decisions-refer"),
    Decision.DECLINE: os.environ.get("UW_DECLINE_QUEUE", "uw-decisions-decline"),
}
QUEUE_RAW_OUTPUT_MAX_CHARS = int(os.environ.get("UW_QUEUE_RAW_OUTPUT_MAX_CHARS", "20000"))
QUEUE_MESSAGE_MAX_BYTES = 60_000
_ENSURED_QUEUES: set[str] = set()


class ProviderError(RuntimeError):
    """UW_LLM_PROVIDER names something this orchestrator cannot build."""


def active_model() -> str:
    """The model this configuration will use — for logs and telemetry."""
    if LLM_PROVIDER == "ollama":
        return os.environ.get("OLLAMA_MODEL", "llama3.1:8b")
    return os.environ.get("AZURE_OPENAI_MODEL", "gpt-4.1")


def check_provider() -> None:
    """Raise if UW_LLM_PROVIDER is not one we support. Called at API startup."""
    if LLM_PROVIDER not in SUPPORTED_PROVIDERS:
        raise ProviderError(
            f"UW_LLM_PROVIDER is set to '{LLM_PROVIDER}'. "
            f"Supported: {', '.join(SUPPORTED_PROVIDERS)}."
        )


async def _build_chat_client(stack: AsyncExitStack):
    """
    Construct the chat client for the configured provider.

    Azure needs a credential whose lifetime spans the run, so it is entered
    on the caller's AsyncExitStack. Ollama needs nothing but a host.
    """
    check_provider()

    if LLM_PROVIDER == "ollama":
        from agent_framework_ollama import OllamaChatClient

        host  = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
        model = active_model()
        logger.info(f"LLM provider: ollama | model={model} | host={host}")
        return OllamaChatClient(host=host, model=model)

    from agent_framework_foundry import FoundryChatClient
    from azure.identity.aio import DefaultAzureCredential

    project_endpoint = os.environ["AZURE_AI_PROJECT_ENDPOINT"]
    model            = active_model()
    credential       = await stack.enter_async_context(DefaultAzureCredential())
    logger.info(f"LLM provider: azure | model={model} | project={project_endpoint}")
    return FoundryChatClient(
        project_endpoint=project_endpoint,
        model=model,
        credential=credential,
    )


# ---------------------------------------------------------------------------
# System prompt — same decision criteria as orchestrator_legacy.py
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """
You are a senior property insurance underwriter at a UK insurer.

Your goal: assess the risk of a broker submission and produce an
underwriting decision: ACCEPT, REFER, or DECLINE.

You have access to these tools. Use them in whatever order you judge
necessary. Keep calling tools until you have sufficient evidence.

Tools available:
  - validate_submission(submission_json): check data completeness and flags
  - get_flood_zone(postcode): flood risk and Flood Re eligibility
  - get_crime_index(postcode): property crime exposure
  - get_claims_history(applicant_name, date_of_birth): verify prior claims
  - get_property_sale_history(postcode, house_number, sum_insured): Land
      Registry sale prices, with a sum-insured plausibility check
  - check_business_registrations(postcode, house_number): companies
      registered at the address
  - search_uw_guidelines(query): search the underwriting guidelines knowledge base

Reading the tool results:
  - Buildings cover is REBUILD cost and excludes land, so a sum insured
    below the last sale price is normal. Act only on the tool's own
    verdict (POSSIBLE_OVERINSURANCE / POSSIBLE_UNDERINSURANCE), and note
    that sale prices are historic and not inflation-adjusted.
  - A registered office is an administrative address, not proof of trading
    at the property. Treat a company hit as a question for the broker
    about occupancy, not as an automatic decline.
  - If a tool reports that data was unavailable (for example crime_band
    DATA_UNAVAILABLE, or check_performed false), treat the risk as
    UNASSESSED and refer. Never read missing data as a low-risk result.

Decision criteria (apply judgement — these are guides, not rigid rules):
  ACCEPT:  No referral triggers. Risk within appetite. No mandatory exclusions.
  REFER:   Any referral trigger present. Borderline flood/crime. Claims anomaly.
           Sum insured above £1,000,000. Uncertain or conflicting signals.
  DECLINE: Risk clearly outside appetite. Examples: Zone 3b flood,
           3+ claims in 5 years, mandatory exclusion applies,
           timber pre-1920 construction + Zone 3a/3b flood.

When you are confident in your decision, return ONLY a JSON object:
{
  "decision": "ACCEPT" | "REFER" | "DECLINE",
  "confidence": "HIGH" | "MEDIUM" | "LOW",
  "rationale": "<plain English explanation, 3-5 sentences>",
  "risk_flags": ["<all material risk flags identified>"],
  "flood_re_eligible": <true | false>,
  "refer_reason": "<reason if REFER, else null>",
  "recommended_premium_loading": <percentage float if ACCEPT with loading, else null>
}
"""


# ---------------------------------------------------------------------------
# RAG tool — a local Python function, not a hosted Foundry tool
#
# orchestrator_legacy.py uses AzureAISearchTool, which only exists inside the
# Foundry Agent Service. MAF has no equivalent, so the retrieval runs here
# and queries the same index over HTTPS. The index carries a vectorizer,
# so the service embeds the query for us — no local embedding call.
# ---------------------------------------------------------------------------

def search_uw_guidelines(query: str) -> str:
    """Search the underwriting guidelines knowledge base.

    Args:
        query: Natural-language description of the guideline to look up,
            e.g. "flood zone 3a appetite" or "timber frame construction".

    Returns:
        The most relevant guideline passages, or a notice if unavailable.
    """
    from azure.search.documents import SearchClient
    from azure.search.documents.models import VectorizableTextQuery
    from azure.identity import DefaultAzureCredential as SyncCredential

    endpoint = os.environ["AZURE_SEARCH_ENDPOINT"]
    index    = os.environ.get("AZURE_SEARCH_INDEX_NAME", "uw-guidelines")

    try:
        client = SearchClient(
            endpoint=endpoint,
            index_name=index,
            credential=SyncCredential(),
        )
        results = client.search(
            search_text=query,
            vector_queries=[
                VectorizableTextQuery(
                    text=query, k_nearest_neighbors=3, fields="content_vector"
                )
            ],
            select=["section", "content"],
            top=3,
        )
        hits = [f"## {r.get('section', 'Guideline')}\n{r.get('content', '')}" for r in results]
    except Exception as e:
        # Index missing or unreachable — tell the agent plainly rather than
        # raising, so it can still reach a (suitably cautious) decision.
        logger.warning(f"Guideline search failed: {e}")
        return (
            f"Guideline search unavailable ({type(e).__name__}). "
            "Proceed using the submission data and risk tools alone, and "
            "reflect the missing guidance in your confidence level."
        )

    if not hits:
        return f"No guidelines matched '{query}'."

    logger.info(f"Guideline search | query='{query}' | hits={len(hits)}")
    return "\n\n".join(hits)


# ---------------------------------------------------------------------------
# Ollama path — deterministic evidence collection + JSON-mode final decision
#
# Smaller local models can narrate the next intended tool call instead of
# emitting a structured tool call. For ollama we collect the same evidence
# explicitly, then ask the model for a single constrained JSON decision.
# ---------------------------------------------------------------------------

def _load_local_guidelines() -> str:
    """Load local underwriting guidelines for Ollama runs without Azure Search."""
    try:
        text = GUIDELINES_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning(f"Local guidelines not found at {GUIDELINES_PATH}")
        return ""
    except Exception as e:
        logger.warning(f"Local guidelines unavailable: {e}")
        return ""

    logger.info(f"Loaded local guidelines: {len(text)} chars")
    return text


def _extract_tool_result_text(result) -> str:
    """Serialise an MCP result to text for the final decision prompt."""
    content = getattr(result, "content", result)
    if isinstance(content, str):
        return content

    if hasattr(content, "__iter__"):
        values = [
            item.text if hasattr(item, "text") else str(item)
            for item in content
        ]
        return json.dumps(values)

    return str(content)


async def _call_mcp_tool(mcp, tool_name: str, tool_args: dict) -> str:
    logger.info(f"Ollama evidence tool: {tool_name}({tool_args})")
    try:
        result = await mcp.call_tool(tool_name, tool_args)
        result_text = _extract_tool_result_text(result)
    except Exception as e:
        logger.warning(f"Ollama evidence tool failed: {tool_name}: {e}")
        result_text = json.dumps({"error": str(e)})

    logger.info(f"Ollama evidence result: {tool_name}: {result_text[:160]}...")
    return result_text


def _house_number(address: str) -> str:
    """
    Pull the building number from the start of a street address.

    Land Registry matches on the primary addressable object name (paon),
    which for most homes is the house number. Without it the sale check
    cannot tell the subject property from its neighbours.
    """
    first = (address or "").strip().split(",")[0].strip().split(" ")
    return first[0] if first and first[0][:1].isdigit() else ""


async def collect_findings(submission: UnderwritingSubmission) -> dict:
    """
    Call the MCP risk tools and return their results as parsed data.

    No LLM involvement: this is the deterministic half of the assessment —
    what a conventional system can produce. Interpretation is the agent's
    job and happens separately in run_underwriting_assessment().
    """
    from fastmcp import Client as MCPClient

    mcp_url = os.environ.get("MCP_RISK_SERVER_URL", "http://127.0.0.1:8001/mcp")
    calls = {
        "flood": ("get_flood_zone", {"postcode": submission.property_postcode}),
        "crime": ("get_crime_index", {"postcode": submission.property_postcode}),
        "claims": (
            "get_claims_history",
            {
                "applicant_name": submission.applicant_name,
                "date_of_birth": submission.date_of_birth,
            },
        ),
        "sale_history": (
            "get_property_sale_history",
            {
                "postcode": submission.property_postcode,
                "house_number": _house_number(submission.property_address),
                "sum_insured": submission.sum_insured,
            },
        ),
        "business": (
            "check_business_registrations",
            {
                "postcode": submission.property_postcode,
                "house_number": _house_number(submission.property_address),
            },
        ),
        "validation": (
            "validate_submission",
            {"submission_json": submission.to_json()},
        ),
    }

    findings: dict = {}
    async with MCPClient(mcp_url) as mcp:
        for key, (tool_name, tool_args) in calls.items():
            raw = await _call_mcp_tool(mcp, tool_name, tool_args)
            findings[key] = _as_dict(raw)
    return findings


def _as_dict(raw: str) -> dict:
    """
    Parse an MCP tool result into a dict.

    _call_mcp_tool returns either a JSON object or a JSON array of text
    blocks (the MCP content envelope), so unwrap one level when needed.
    """
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {"error": "Tool returned unparseable output", "raw": raw}

    if isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                try:
                    inner = json.loads(item)
                except (TypeError, ValueError):
                    continue
                if isinstance(inner, dict):
                    return inner
        return {"error": "Tool returned no object", "raw": raw}

    return value if isinstance(value, dict) else {"value": value}


async def _collect_ollama_evidence(
    submission: UnderwritingSubmission,
    mcp_url: str,
) -> dict:
    """Collect required risk signals before asking Ollama for a decision."""
    from fastmcp import Client as MCPClient

    submission_json = submission.to_json()
    evidence = {
        "validate_submission": None,
        "get_flood_zone": None,
        "get_crime_index": None,
        "get_claims_history": None,
        "get_property_sale_history": None,
        "check_business_registrations": None,
    }

    async with MCPClient(mcp_url) as mcp:
        evidence["validate_submission"] = await _call_mcp_tool(
            mcp, "validate_submission", {"submission_json": submission_json}
        )
        evidence["get_flood_zone"] = await _call_mcp_tool(
            mcp, "get_flood_zone", {"postcode": submission.property_postcode}
        )
        evidence["get_crime_index"] = await _call_mcp_tool(
            mcp, "get_crime_index", {"postcode": submission.property_postcode}
        )
        evidence["get_claims_history"] = await _call_mcp_tool(
            mcp,
            "get_claims_history",
            {
                "applicant_name": submission.applicant_name,
                "date_of_birth": submission.date_of_birth,
            },
        )
        evidence["get_property_sale_history"] = await _call_mcp_tool(
            mcp,
            "get_property_sale_history",
            {
                "postcode": submission.property_postcode,
                "house_number": _house_number(submission.property_address),
                "sum_insured": submission.sum_insured,
            },
        )
        evidence["check_business_registrations"] = await _call_mcp_tool(
            mcp,
            "check_business_registrations",
            {
                "postcode": submission.property_postcode,
                "house_number": _house_number(submission.property_address),
            },
        )

    return evidence


def _build_ollama_decision_prompt(
    submission: UnderwritingSubmission,
    evidence: dict,
    guidelines: str,
) -> list[dict]:
    schema = {
        "decision": "ACCEPT | REFER | DECLINE",
        "confidence": "HIGH | MEDIUM | LOW",
        "rationale": "plain English explanation, 3-5 sentences",
        "risk_flags": ["all material risk flags identified"],
        "flood_re_eligible": "true | false",
        "refer_reason": "reason if REFER, else null",
        "recommended_premium_loading": "percentage float if ACCEPT with loading, else null",
    }

    system = (
        "You are a senior property insurance underwriter at a UK insurer. "
        "Make a final underwriting decision from the supplied submission, "
        "tool evidence, and guidelines. Return exactly one valid JSON object. "
        "Do not mention tool calls. Do not include markdown. Do not include prose "
        "outside the JSON object."
    )
    user = (
        "Decision criteria:\n"
        "ACCEPT: no referral triggers, risk within appetite, no mandatory exclusions.\n"
        "REFER: any referral trigger, borderline flood/crime, claims anomaly, "
        "sum insured above £1,000,000, uncertain or conflicting signals.\n"
        "DECLINE: clearly outside appetite, including Zone 3b flood, 3+ claims "
        "in 5 years, mandatory exclusion, or timber pre-1920 construction plus "
        "Zone 3a/3b flood.\n\n"
        f"Required JSON shape:\n{json.dumps(schema, indent=2)}\n\n"
        f"Submission JSON:\n{submission.to_json()}\n\n"
        f"Tool evidence JSON:\n{json.dumps(evidence, indent=2)}\n\n"
        f"Underwriting guidelines:\n{guidelines or 'No local guidelines available.'}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _ollama_response_value(response, key: str, default=None):
    if hasattr(response, key):
        return getattr(response, key)
    if hasattr(response, "get"):
        return response.get(key, default)
    return default


async def _run_ollama_assessment_async(
    submission: UnderwritingSubmission,
) -> tuple[str, int, int, int]:
    """Run an Ollama-backed assessment and return raw JSON text plus usage data."""
    import ollama

    model = active_model()
    host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
    mcp_url = os.environ.get("MCP_RISK_SERVER_URL", "http://127.0.0.1:8001/mcp")

    logger.info(f"Ollama assessment | model={model} | host={host} | mcp={mcp_url}")

    run_start_ms = int(time.time() * 1000)
    evidence = await _collect_ollama_evidence(submission, mcp_url)
    messages = _build_ollama_decision_prompt(
        submission=submission,
        evidence=evidence,
        guidelines=_load_local_guidelines(),
    )

    client = ollama.Client(host=host)
    response = await asyncio.to_thread(
        client.chat,
        model=model,
        messages=messages,
        format="json",
        options={
            "temperature": 0,
            "num_ctx": OLLAMA_MAX_CONTEXT,
            "num_predict": OLLAMA_NUM_PREDICT,
        },
    )
    latency_ms = int(time.time() * 1000) - run_start_ms

    message = getattr(response, "message", None)
    if message is not None:
        raw_output = getattr(message, "content", None)
    else:
        raw_output = (_ollama_response_value(response, "message", {}) or {}).get("content", "")
    prompt_tokens = int(_ollama_response_value(response, "prompt_eval_count", 0) or 0)
    completion_tokens = int(_ollama_response_value(response, "eval_count", 0) or 0)

    return raw_output or "", prompt_tokens, completion_tokens, latency_ms


# ---------------------------------------------------------------------------
# The agent run — loop executes in THIS process
# ---------------------------------------------------------------------------

async def _run_assessment_async(
    submission: UnderwritingSubmission,
) -> tuple[str, int, int, int]:
    """Run the agent loop locally. Returns (raw_output, prompt_tok, completion_tok, latency_ms)."""

    model   = active_model()
    mcp_url = os.environ.get("MCP_RISK_SERVER_URL", "http://127.0.0.1:8001/mcp")

    logger.info(f"MCP server (dialled from this process): {mcp_url}")

    # approval_mode="never_require" auto-approves tool calls, so the loop
    # runs unattended.
    mcp_tool = MCPStreamableHTTPTool(
        name="uw_risk_tools",
        url=mcp_url,
        approval_mode="never_require",
        allowed_tools=[
            "validate_submission",
            "get_flood_zone",
            "get_crime_index",
            "get_claims_history",
            "get_property_sale_history",
            "check_business_registrations",
        ],
    )

    prompt = (
        "Please assess this broker submission and return your decision:\n\n"
        + submission.to_json()
    )

    async with AsyncExitStack() as stack:
        chat_client = await _build_chat_client(stack)

        # The agent is an ordinary local object — nothing is registered
        # remotely, so there is nothing to clean up afterwards.
        agent = Agent(
            client=chat_client,
            instructions=SYSTEM_PROMPT,
            name="uw-risk-agent",
            tools=[mcp_tool, search_uw_guidelines],
        )

        with tracer.start_as_current_span("uw_agent_run") as span:
            span.set_attribute("broker_reference", submission.broker_reference)
            span.set_attribute("property_postcode", submission.property_postcode)
            span.set_attribute("model", model)
            span.set_attribute("llm_provider", LLM_PROVIDER)
            span.set_attribute("framework", "microsoft-agent-framework")

            run_start_ms = int(time.time() * 1000)
            async with mcp_tool:              # opens the MCP session
                response = await agent.run(prompt)
            latency_ms = int(time.time() * 1000) - run_start_ms

    usage = getattr(response, "usage_details", None) or {}
    prompt_tokens     = int(usage.get("input_token_count", 0) or 0) if hasattr(usage, "get") else 0
    completion_tokens = int(usage.get("output_token_count", 0) or 0) if hasattr(usage, "get") else 0

    return response.text or "", prompt_tokens, completion_tokens, latency_ms


# ---------------------------------------------------------------------------
# Main entry point — same signature as orchestrator.run_underwriting_assessment
# ---------------------------------------------------------------------------

def _mock_decision(
    submission: UnderwritingSubmission,
    elapsed_ms: int = 0,
) -> UnderwritingDecision:
    """Return a random, internally consistent decision without calling an LLM."""
    selected = random.choice(tuple(Decision))
    outcomes = {
        Decision.ACCEPT: {
            "confidence": "HIGH",
            "rationale": "Mock assessment completed. The risk is within appetite and can be accepted.",
            "risk_flags": [],
            "flood_re_eligible": True,
            "refer_reason": None,
            "recommended_premium_loading": None,
        },
        Decision.DECLINE: {
            "confidence": "HIGH",
            "rationale": "Mock assessment completed. The risk is outside underwriting appetite and is declined.",
            "risk_flags": ["MOCK_OUTSIDE_APPETITE"],
            "flood_re_eligible": False,
            "refer_reason": None,
            "recommended_premium_loading": None,
        },
        Decision.REFER: {
            "confidence": "MEDIUM",
            "rationale": "Mock assessment completed. The risk requires review by a human underwriter.",
            "risk_flags": ["MOCK_REVIEW_REQUIRED"],
            "flood_re_eligible": False,
            "refer_reason": "Mock decision selected for human review.",
            "recommended_premium_loading": None,
        },
    }
    fields = outcomes[selected]
    raw_output = json.dumps(
        {
            "mock_decision": True,
            "decision": selected.value,
            **fields,
        }
    )

    return UnderwritingDecision(
        decision=selected,
        broker_reference=submission.broker_reference,
        raw_agent_output=raw_output,
        processing_time_ms=elapsed_ms,
        **fields,
    )


def run_underwriting_assessment(
    submission: UnderwritingSubmission,
) -> UnderwritingDecision:
    """
    Run the agentic underwriting assessment with Microsoft Agent Framework.

    Synchronous wrapper so Streamlit and the original call sites are
    unaffected; the loop underneath is async.
    """
    start_ms = int(time.time() * 1000)
    logger.info(
        f"Assessment started | provider={LLM_PROVIDER} | model={active_model()} | "
        f"broker_ref={submission.broker_reference} | "
        f"postcode={submission.property_postcode}"
    )

    model = active_model()

    if MOCK_DECISION:
        decision = _mock_decision(
            submission,
            elapsed_ms=int(time.time() * 1000) - start_ms,
        )
        logger.info(
            f"Mock assessment complete | decision={decision.decision} | "
            f"confidence={decision.confidence}"
        )
        _enqueue_decision(submission, decision)
        if decision.decision == Decision.REFER:
            _handle_refer(submission, decision)
        return decision

    try:
        runner = (
            _run_ollama_assessment_async
            if LLM_PROVIDER == "ollama"
            else _run_assessment_async
        )
        raw_output, prompt_tokens, completion_tokens, latency_ms = asyncio.run(
            runner(submission)
        )
    except Exception as e:
        track_llm_call(
            model=model,
            prompt_tokens=0,
            completion_tokens=0,
            latency_ms=int(time.time() * 1000) - start_ms,
            success=False,
            broker_reference=submission.broker_reference,
        )
        logger.error(f"Agent run failed: {e}")
        raise RuntimeError(f"Agent run failed: {e}") from e

    track_llm_call(
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=latency_ms,
        success=True,
        broker_reference=submission.broker_reference,
    )
    logger.info(
        f"Telemetry emitted | prompt_tokens={prompt_tokens} | "
        f"completion_tokens={completion_tokens} | latency_ms={latency_ms}"
    )
    logger.info(f"Raw output length: {len(raw_output)} chars")

    elapsed_ms = int(time.time() * 1000) - start_ms
    decision   = _parse_decision(raw_output, submission, elapsed_ms)

    logger.info(
        f"Assessment complete | decision={decision.decision} | "
        f"confidence={decision.confidence} | elapsed_ms={elapsed_ms}"
    )

    _enqueue_decision(submission, decision)

    if decision.decision == Decision.REFER:
        _handle_refer(submission, decision)

    return decision


# ---------------------------------------------------------------------------
# Human-in-the-loop handoff and decision parsing
#
# Copied from the legacy orchestrator so this module stands alone;
# api/orchestrator_legacy.py is retained for reference but not imported.
# ---------------------------------------------------------------------------

def _handle_refer(
    submission: UnderwritingSubmission,
    decision: UnderwritingDecision,
) -> None:
    """
    Stub: escalate to human underwriter queue.
    Production: write to Azure Service Bus, create workflow task,
                persist full audit trace to Cosmos DB.
    """
    logger.info(
        f"REFER | broker_ref={submission.broker_reference} | "
        f"reason={decision.refer_reason}"
    )
    print(f"\n[HUMAN REVIEW QUEUE] Case {submission.broker_reference} referred.")
    print(f"Reason: {decision.refer_reason}\n")


def _truncate(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[:max_chars] + "...[truncated]"


def _build_queue_message(
    submission: UnderwritingSubmission,
    decision: UnderwritingDecision,
) -> str:
    raw_agent_output = _truncate(
        decision.raw_agent_output or "",
        QUEUE_RAW_OUTPUT_MAX_CHARS,
    )
    payload = {
        "schema_version": "1.0",
        "event_type": "underwriting_decision",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "broker_reference": decision.broker_reference,
        "decision": decision.decision.value,
        "confidence": decision.confidence,
        "rationale": decision.rationale,
        "risk_flags": [str(flag) for flag in decision.risk_flags],
        "flood_re_eligible": decision.flood_re_eligible,
        "refer_reason": decision.refer_reason,
        "recommended_premium_loading": decision.recommended_premium_loading,
        "processing_time_ms": decision.processing_time_ms,
        "provider": LLM_PROVIDER,
        "model": active_model(),
        "submission": {
            "applicant_name": submission.applicant_name,
            "date_of_birth": submission.date_of_birth,
            "occupation": submission.occupation,
            "property_address": submission.property_address,
            "property_postcode": submission.property_postcode,
            "property_type": submission.property_type,
            "year_built": submission.year_built,
            "construction": submission.construction,
            "num_storeys": submission.num_storeys,
            "product_type": submission.product_type,
            "sum_insured": submission.sum_insured,
            "policy_start_date": submission.policy_start_date,
            "claims_last_5_years": submission.claims_last_5_years,
            "prior_claim_types": submission.prior_claim_types,
            "outstanding_claims": submission.outstanding_claims,
            "special_conditions": submission.special_conditions,
        },
        "raw_agent_output": raw_agent_output,
    }

    message = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    if len(message.encode("utf-8")) <= QUEUE_MESSAGE_MAX_BYTES:
        return message

    payload["raw_agent_output"] = _truncate(decision.raw_agent_output or "", 4000)
    message = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    if len(message.encode("utf-8")) <= QUEUE_MESSAGE_MAX_BYTES:
        return message

    payload["raw_agent_output"] = "[omitted: queue message size limit]"
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True)


def _build_queue_client(
    queue_name: str,
    connection_string: str,
    storage_account: str,
):
    from azure.identity import DefaultAzureCredential as SyncCredential
    from azure.storage.queue import QueueClient

    if connection_string:
        return QueueClient.from_connection_string(
            connection_string,
            queue_name=queue_name,
        )

    return QueueClient(
        account_url=f"https://{storage_account}.queue.core.windows.net",
        queue_name=queue_name,
        credential=SyncCredential(),
    )


def _ensure_decision_queues(connection_string: str, storage_account: str) -> None:
    from azure.core.exceptions import ResourceExistsError

    for queue_name in set(DECISION_QUEUE_NAMES.values()):
        if queue_name in _ENSURED_QUEUES:
            continue

        queue_client = _build_queue_client(
            queue_name=queue_name,
            connection_string=connection_string,
            storage_account=storage_account,
        )
        try:
            queue_client.create_queue()
            logger.info(f"Decision queue created | queue={queue_name}")
        except ResourceExistsError:
            logger.info(f"Decision queue already exists | queue={queue_name}")
        _ENSURED_QUEUES.add(queue_name)


def _enqueue_decision(
    submission: UnderwritingSubmission,
    decision: UnderwritingDecision,
) -> None:
    """
    Put the decision onto its Azure Storage Queue.

    Queue dispatch is downstream integration. A dispatch failure must not
    change the underwriting outcome returned to the caller.
    """
    connection_string = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "").strip()
    storage_account = os.environ.get("AZURE_STORAGE_ACCOUNT_NAME", "").strip()
    if not connection_string and not storage_account:
        logger.info(
            "Decision queue dispatch skipped: neither AZURE_STORAGE_CONNECTION_STRING "
            "nor AZURE_STORAGE_ACCOUNT_NAME is set"
        )
        return

    queue_name = DECISION_QUEUE_NAMES[decision.decision]
    try:
        _ensure_decision_queues(connection_string, storage_account)
        queue_client = _build_queue_client(
            queue_name=queue_name,
            connection_string=connection_string,
            storage_account=storage_account,
        )

        queue_client.send_message(_build_queue_message(submission, decision))
        logger.info(
            f"Decision queued | broker_ref={submission.broker_reference} | "
            f"decision={decision.decision.value} | queue={queue_name}"
        )
    except Exception as e:
        logger.error(
            f"Decision queue dispatch failed | broker_ref={submission.broker_reference} | "
            f"decision={decision.decision.value} | queue={queue_name} | error={e}"
        )


def _parse_decision(
    raw: str,
    submission: UnderwritingSubmission,
    elapsed_ms: int,
) -> UnderwritingDecision:
    """
    Parse the LLM's final JSON output into a typed UnderwritingDecision.

    Handles three common LLM output patterns:
      1. Pure JSON                   {"decision": ...}
      2. Markdown fenced             ```json\n{"decision": ...}\n```
      3. JSON wrapped in prose       "Here is my decision: {...}"

    Falls back to REFER — safe default for regulated context.
    """
    try:
        cleaned = (raw.strip()
                   .removeprefix("```json")
                   .removeprefix("```")
                   .removesuffix("```")
                   .strip())

        start = cleaned.find("{")
        end   = cleaned.rfind("}") + 1
        if start >= 0 and end > start:
            cleaned = cleaned[start:end]

        parsed = json.loads(cleaned)
        return UnderwritingDecision(
            decision=Decision(parsed["decision"]),
            confidence=parsed.get("confidence", "LOW"),
            rationale=parsed.get("rationale", ""),
            risk_flags=parsed.get("risk_flags", []),
            flood_re_eligible=parsed.get("flood_re_eligible", False),
            refer_reason=parsed.get("refer_reason"),
            recommended_premium_loading=parsed.get("recommended_premium_loading"),
            broker_reference=submission.broker_reference,
            raw_agent_output=raw,
            processing_time_ms=elapsed_ms,
        )
    except Exception as e:
        logger.warning(f"Decision parse failed: {e} — defaulting to REFER")
        return UnderwritingDecision(
            decision=Decision.REFER,
            confidence="LOW",
            rationale="Automated decision unavailable — system error. Referred for human review.",
            risk_flags=["SYSTEM_ERROR"],
            flood_re_eligible=False,
            refer_reason="Parse error in decision agent output.",
            recommended_premium_loading=None,
            broker_reference=submission.broker_reference,
            raw_agent_output=raw,
            processing_time_ms=elapsed_ms,
        )


# ---------------------------------------------------------------------------
# Smoke test — python -m api.orchestrator
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import dotenv
    from azure.monitor.opentelemetry import configure_azure_monitor

    dotenv.load_dotenv()
    configure_azure_monitor(
        connection_string=os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")
    )

    os.environ["AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED"] = "true"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )

    test_submission = UnderwritingSubmission(
        applicant_name="Jane Smith",
        date_of_birth="1978-06-15",
        occupation="Teacher",
        property_address="12 Riverside Close, Bristol",
        property_postcode="BS1 4DJ",
        property_type="detached",
        year_built=1912,
        construction="timber",
        num_storeys=2,
        product_type="combined",
        sum_insured=425000.0,
        policy_start_date="2026-05-01",
        claims_last_5_years=2,
        prior_claim_types=["escape_of_water", "subsidence"],
        outstanding_claims=False,
        broker_reference="BRK-2026-00142",
    )

    result = run_underwriting_assessment(test_submission)

    print("\n" + "=" * 60)
    print(f"DECISION   : {result.decision.value}")
    print(f"CONFIDENCE : {result.confidence}")
    print(f"RATIONALE  : {result.rationale}")
    print(f"FLAGS      : {', '.join(result.risk_flags) or 'None'}")
    print(f"FLOOD RE   : {'Yes' if result.flood_re_eligible else 'No'}")
    if result.recommended_premium_loading:
        print(f"LOADING    : +{result.recommended_premium_loading:.1f}%")
    if result.refer_reason:
        print(f"REFER NOTE : {result.refer_reason}")
    print(f"TIME       : {result.processing_time_ms}ms")
    print("=" * 60)
