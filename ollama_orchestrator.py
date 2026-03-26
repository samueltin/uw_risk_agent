"""
Underwriting Risk Assessment — Remote Ollama Orchestrator
----------------------------------------------------------
Runs the underwriting agent using Ollama on a remote Ubuntu PC
(GTX 1070, 8GB VRAM) instead of Azure OpenAI. Uses:
  - Ollama (qwen2.5:14b)      on Ubuntu PC at 192.168.2.250
  - Inline UW guidelines      instead of Azure AI Search RAG
  - risk_server_v3.py (MCP)   unchanged — real EA + Police APIs
    (runs locally on this Mac)

Network layout:
  MacBook (this machine)          Ubuntu PC (192.168.2.250)
  ─────────────────────           ──────────────────────────
  ollama_orchestrator.py  ──────► Ollama + qwen2.5:14b (GPU)
  risk_server_v3.py (MCP)
    └── EA flood API
    └── Police crime API

Run:
  Ubuntu PC:  ollama serve   (already running as systemd service)
  Terminal 1: python mcp_servers/risk_server_v3.py
  Terminal 2: python ollama_orchestrator.py

Requirements (Mac):
  pip install ollama fastmcp httpx python-dotenv

Requirements (Ubuntu PC):
  ollama pull qwen2.5:14b
"""

import json
import time
import asyncio
import logging
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

import ollama
from fastmcp import Client as MCPClient

from models.submission import UnderwritingSubmission
from models.decision import UnderwritingDecision, Decision

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OLLAMA_MODEL    = "qwen2.5:14b"
OLLAMA_HOST     = "http://192.168.2.250:11434"   # Ubuntu PC with GTX 1070
MCP_SERVER_URL  = "http://127.0.0.1:8001/mcp"
MAX_ITERATIONS  = 10      # guard against infinite tool call loops
GUIDELINES_PATH = Path(__file__).parent / "knowledge_base" / "uw_guidelines.md"

# qwen2.5:14b supports 32k context — plenty of room for:
# guidelines (~2k tokens) + submission (~200) + tool results (~1k)
CTX_WINDOW = 32768

# ---------------------------------------------------------------------------
# Load UW guidelines at startup (replaces Azure AI Search RAG)
# ---------------------------------------------------------------------------

def _load_guidelines() -> str:
    if not GUIDELINES_PATH.exists():
        logger.warning(f"Guidelines not found at {GUIDELINES_PATH} — using empty string")
        return ""
    text = GUIDELINES_PATH.read_text(encoding="utf-8")
    logger.info(f"Loaded guidelines: {len(text)} chars (~{len(text)//4} tokens)")
    return text


GUIDELINES = _load_guidelines()

# ---------------------------------------------------------------------------
# System prompt — guidelines injected inline
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = f"""You are a senior property underwriter at a UK insurer.

Your goal: assess a broker submission and return an underwriting decision.

--- UNDERWRITING GUIDELINES ---
{GUIDELINES}
--- END GUIDELINES ---

You have four tools. Use them in whatever order you judge necessary:
  - validate_submission(submission_json): check completeness and flag issues
  - get_flood_zone(postcode): flood risk and Flood Re eligibility
  - get_crime_index(postcode): property crime exposure
  - get_claims_history(applicant_name, date_of_birth): verify prior claims

Keep calling tools until you have enough evidence. Then return ONLY a JSON
object — no preamble, no markdown fences, just the JSON:

{{
  "decision": "ACCEPT" | "REFER" | "DECLINE",
  "confidence": "HIGH" | "MEDIUM" | "LOW",
  "rationale": "<plain English, 2-4 sentences>",
  "risk_flags": ["<flag>", ...],
  "flood_re_eligible": true | false,
  "refer_reason": "<reason if REFER, else null>",
  "recommended_premium_loading": <float if ACCEPT with loading, else null>
}}"""


# ---------------------------------------------------------------------------
# MCP tool definitions helper
# ---------------------------------------------------------------------------

async def _get_tool_definitions() -> list[dict]:
    """Fetch tool schemas from the MCP server and convert to Ollama format."""
    async with MCPClient(MCP_SERVER_URL) as mcp:
        tools = await mcp.list_tools()

    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description or "",
                "parameters": t.inputSchema or {"type": "object", "properties": {}},
            }
        }
        for t in tools
    ]


# ---------------------------------------------------------------------------
# Main agentic loop — helpers
# ---------------------------------------------------------------------------

def _build_assistant_message(msg) -> dict:
    return {
        "role": "assistant",
        "content": msg.content or "",
        "tool_calls": [
            {"function": {"name": tc.function.name, "arguments": tc.function.arguments}}
            for tc in (msg.tool_calls or [])
        ],
    }


def _extract_result_text(result) -> str:
    """Serialise an MCP result (iterable of content items, or scalar) to a string."""
    if not hasattr(result, '__iter__'):
        return str(result)
    return json.dumps([
        item.text if hasattr(item, 'text') else str(item)
        for item in result
    ])


async def _execute_tool_call(mcp, tc) -> dict:
    """Call one MCP tool and return a 'tool' role message for the history."""
    tool_name = tc.function.name
    tool_args = tc.function.arguments or {}
    logger.info(f"  Tool call: {tool_name}({tool_args})")
    try:
        result = await mcp.call_tool(tool_name, tool_args)
        result_text = _extract_result_text(result)
    except Exception as e:
        logger.warning(f"  Tool error: {e}")
        result_text = json.dumps({"error": str(e)})
    logger.info(f"  Result: {result_text[:120]}...")
    return {"role": "tool", "content": result_text, "name": tool_name}


# ---------------------------------------------------------------------------
# Main agentic loop
# ---------------------------------------------------------------------------

async def _run_loop(
    submission: UnderwritingSubmission,
    tool_defs: list[dict],
) -> str:
    """
    Core agentic loop: Ollama LLM ↔ MCP tools.

    Each iteration:
      1. Send messages + tool definitions to Ollama
      2. If LLM returns tool calls → execute via MCP → append results → repeat
      3. If LLM returns text (no tool calls) → that's the final decision
    """
    client = ollama.Client(host=OLLAMA_HOST)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Please assess this broker submission and return your decision:\n\n"
                + submission.to_prompt_str()
                + "\n\nSubmission JSON for validate_submission:\n"
                + submission.to_json()
            ),
        },
    ]

    async with MCPClient(MCP_SERVER_URL) as mcp:
        for iteration in range(1, MAX_ITERATIONS + 1):
            logger.info(f"Loop iteration {iteration}/{MAX_ITERATIONS}")

            response = client.chat(
                model=OLLAMA_MODEL,
                messages=messages,
                tools=tool_defs,
                options={
                    "temperature": 0.1,
                    "num_ctx": CTX_WINDOW,
                    "num_predict": 2048,
                },
            )

            msg = response.message
            messages.append(_build_assistant_message(msg))

            if not msg.tool_calls:
                logger.info("LLM returned final response (no tool calls)")
                return msg.content or ""

            for tc in msg.tool_calls:
                messages.append(await _execute_tool_call(mcp, tc))

    logger.warning("Max iterations reached — returning last assistant message")
    return next(
        (m["content"] for m in reversed(messages) if m["role"] == "assistant"),
        "",
    )


# ---------------------------------------------------------------------------
# Parse decision
# ---------------------------------------------------------------------------

def _parse_decision(
    raw: str,
    submission: UnderwritingSubmission,
    elapsed_ms: int,
) -> UnderwritingDecision:
    """
    Parse the LLM's final JSON output into a typed UnderwritingDecision.
    Falls back to REFER if parsing fails — safe default for regulated context.
    """
    try:
        cleaned = (raw.strip()
                   .removeprefix("```json")
                   .removeprefix("```")
                   .removesuffix("```")
                   .strip())
        # Handle models that wrap JSON in extra text
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
        logger.warning(f"Decision parse failed: {e}")
        return UnderwritingDecision(
            decision=Decision.REFER,
            confidence="LOW",
            rationale=f"Local model output could not be parsed. Raw: {raw[:300]}",
            risk_flags=["PARSE_ERROR"],
            flood_re_eligible=False,
            refer_reason="Automated decision unavailable — parse error.",
            recommended_premium_loading=None,
            broker_reference=submission.broker_reference,
            raw_agent_output=raw,
            processing_time_ms=elapsed_ms,
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_underwriting_assessment_local(
    submission: UnderwritingSubmission,
) -> UnderwritingDecision:
    """
    Run a fully local underwriting assessment.
    No Azure required — Ollama + MCP server only.
    """
    start_ms = int(time.time() * 1000)
    logger.info(
        f"Assessment started | "
        f"model={OLLAMA_MODEL} | "
        f"ollama={OLLAMA_HOST} | "
        f"broker_ref={submission.broker_reference} | "
        f"postcode={submission.property_postcode}"
    )

    # Fetch tool definitions from the live MCP server
    try:
        tool_defs = await _get_tool_definitions()
        logger.info(f"Loaded {len(tool_defs)} tools from MCP server")
    except Exception as e:
        logger.error(f"Cannot connect to MCP server at {MCP_SERVER_URL}: {e}")
        raise RuntimeError(
            f"MCP server not reachable at {MCP_SERVER_URL}. "
            "Start it with: python mcp_servers/risk_server_v3.py"
        ) from e

    # Run the agentic loop
    raw_output = await _run_loop(submission, tool_defs)

    elapsed_ms = int(time.time() * 1000) - start_ms
    decision = _parse_decision(raw_output, submission, elapsed_ms)

    logger.info(
        f"Assessment complete | decision={decision.decision} | "
        f"confidence={decision.confidence} | elapsed_ms={elapsed_ms}"
    )
    return decision


# ---------------------------------------------------------------------------
# Smoke test — python ollama_orchestrator.py
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import dotenv
    dotenv.load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )

    print(f"\nUsing model : {OLLAMA_MODEL}")
    print(f"Ollama host : {OLLAMA_HOST}")
    print(f"MCP server  : {MCP_SERVER_URL}")
    print(f"Guidelines  : {len(GUIDELINES)} chars loaded")
    print(f"Context win : {CTX_WINDOW} tokens\n")

    # Quick connectivity check before running assessment
    try:
        client = ollama.Client(host=OLLAMA_HOST)
        models = client.list()
        available = [m.model for m in models.models]
        print(f"Connected to Ollama at {OLLAMA_HOST}")
        print(f"Available models: {', '.join(available)}\n")
        if not any(OLLAMA_MODEL in m for m in available):
            print(f"WARNING: {OLLAMA_MODEL} not found on remote host.")
            print(f"Run on Ubuntu PC: ollama pull {OLLAMA_MODEL}\n")
    except Exception as e:
        print(f"ERROR: Cannot connect to Ollama at {OLLAMA_HOST}")
        print(f"Details: {e}")
        print("\nCheck:")
        print("  1. Ubuntu PC is on and Ollama is running")
        print("  2. OLLAMA_HOST is set to 0.0.0.0 in systemd override")
        print("  3. Firewall allows port 11434 (sudo ufw allow 11434/tcp)")
        exit(1)

    # High-risk test case:
    # BS1 4DJ = Zone 3a flood (static layer), HIGH crime
    # timber 1912 = TIMBER_PRE_1920_HIGH_RISK flag
    # 2 claims including subsidence
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
        broker_reference="BRK-REMOTE-001",
    )

    result = asyncio.run(run_underwriting_assessment_local(test_submission))

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
    print("\nRaw output:")
    print(result.raw_agent_output)
