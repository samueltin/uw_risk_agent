"""
Underwriting Risk Assessment — Agentic Orchestrator
-----------------------------------------------------
True agentic design: one LLM with all tools registered.
The LLM runs in a loop, deciding which tools to call and when,
until it has enough evidence to produce a final decision.

"An LLM agent runs tools in a loop to achieve a goal."
                                        — Simon Willison

What changed from v1 (pipeline) to v2 (agent):
  v1: Python code decided the sequence — always applicant → risk → rules → decision.
      The LLM only did text generation at each fixed step.
  v2: The LLM decides the sequence. It evaluates its current state
      each iteration and picks the next tool. It stops when it has
      enough evidence. Python only provides the tools and the goal.

Microsoft Agent Framework handles the loop automatically via
create_and_process_run() — it cycles: LLM → tool call → result → LLM
until the LLM produces a final text response with no more tool calls.
"""

import os
import json
import time
import logging
from azure.ai.projects import AIProjectClient
from azure.ai.agents.models import McpTool, AzureAISearchTool
from azure.identity import DefaultAzureCredential

from models.submission import UnderwritingSubmission
from models.decision import UnderwritingDecision, Decision

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# System prompt — defines the goal and decision criteria.
# The LLM owns the reasoning; we only define what "done" looks like.
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
  - search_uw_guidelines: search the underwriting guidelines knowledge base

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
# Human-in-the-loop handoff — called when decision == REFER
# ---------------------------------------------------------------------------

def _handle_refer(submission: UnderwritingSubmission, decision: UnderwritingDecision) -> None:
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


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_underwriting_assessment(
    submission: UnderwritingSubmission,
) -> UnderwritingDecision:
    """
    Run the agentic underwriting assessment.

    The LLM receives the submission and all tools. It then loops —
    calling tools, evaluating results, calling more tools if needed —
    until it reaches a confident decision. We never prescribe the sequence.
    """
    start_ms = int(time.time() * 1000)
    logger.info(
        f"Assessment started | broker_ref={submission.broker_reference} | "
        f"postcode={submission.property_postcode}"
    )

    client = AIProjectClient(
        endpoint=os.environ["AZURE_AI_PROJECT_ENDPOINT"],
        credential=DefaultAzureCredential()
    )

    # ------------------------------------------------------------------
    # Register all tools — the LLM chooses which to call and when
    # ------------------------------------------------------------------

    # MCP server exposes: validate_submission, get_flood_zone,
    #                     get_crime_index, get_claims_history
    mcp_tool = McpTool(
        server_label="uw-risk-tools",
        server_url=os.environ.get("MCP_RISK_SERVER_URL", "http://127.0.0.1:8001/mcp"),
        allowed_tools=[
            "validate_submission",
            "get_flood_zone",
            "get_crime_index",
            "get_claims_history",
        ],
    )

    # Azure AI Search exposes the UW guidelines knowledge base via RAG
    search_tool = AzureAISearchTool(
        index_connection_id=os.environ["AZURE_SEARCH_CONNECTION_ID"],
        index_name=os.environ.get("AZURE_SEARCH_INDEX_NAME", "uw-guidelines"),
    )

    all_tool_defs = mcp_tool.definitions + search_tool.definitions
    all_tool_resources = {**mcp_tool.resources, **search_tool.resources}

    # ------------------------------------------------------------------
    # Hand off to the agent — this single call IS the agent loop.
    # Agent Framework cycles: LLM → tool call → result → LLM → ...
    # until the LLM stops calling tools and returns a final response.
    #
    # User message uses JSON only — to_prompt_str() was removed as it
    # duplicated the same data, wasting input tokens every iteration.
    # The LLM reads JSON natively and extracts tool arguments from it.
    # ------------------------------------------------------------------
    response = client.agents.create_and_process_run(
        model=os.environ.get("AZURE_OPENAI_MODEL", "gpt-4.1"),
        system_message=SYSTEM_PROMPT,
        user_message=(
            "Please assess this broker submission and return your decision:\n\n"
            + submission.to_json()
        ),
        tools=all_tool_defs,
        tool_resources=all_tool_resources,
    )

    elapsed_ms = int(time.time() * 1000) - start_ms
    raw_output = response.messages.get_last_text_message_by_role("assistant")

    # ------------------------------------------------------------------
    # Parse the LLM's final JSON decision into a typed object
    # ------------------------------------------------------------------
    decision = _parse_decision(raw_output, submission, elapsed_ms)

    logger.info(
        f"Assessment complete | decision={decision.decision} | "
        f"confidence={decision.confidence} | elapsed_ms={elapsed_ms}"
    )

    # Route REFER cases to human review
    if decision.decision == Decision.REFER:
        _handle_refer(submission, decision)

    return decision


def _parse_decision(
    raw: str,
    submission: UnderwritingSubmission,
    elapsed_ms: int,
) -> UnderwritingDecision:
    """
    Parse the LLM's final JSON output into a typed UnderwritingDecision.

    Handles three common LLM output patterns:
      1. Pure JSON                      {"decision": ...}
      2. Markdown fenced               ```json\n{"decision": ...}\n```
      3. JSON wrapped in prose         "Here is my decision: {...}"

    Falls back to REFER if parsing fails — safe default for regulated context.
    """
    try:
        # Strip markdown fences
        cleaned = (raw.strip()
                   .removeprefix("```json")
                   .removeprefix("```")
                   .removesuffix("```")
                   .strip())

        # Extract JSON object if wrapped in surrounding prose
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
# Smoke test — python orchestrator.py
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import dotenv
    dotenv.load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )

    # High-risk case: Zone 3a postcode, timber pre-1920, 2 claims
    # Expected: REFER or DECLINE
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
