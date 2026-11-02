"""
Run the twelve Postman test cases against the live API and check each decision.

The Postman collection is the single source of the submissions and their
expected outcomes, so this reads it rather than keeping a second copy that
could drift. The expected decision comes from each case name:
"— expect DECLINE" allows DECLINE, "— REFER or DECLINE" allows either.

Needs the full stack running (./start.sh): MCP server, API and a real LLM.
Each case takes roughly 15-40s on Azure, so a full run takes a few minutes.
Every assessment is dispatched to the decision queues as normal.

    pytest tests/test_assess_cases.py -v
    pytest tests/test_assess_cases.py -k 02          # one case
    UW_API_URL=http://host:8010 pytest tests/test_assess_cases.py
"""

import json
import os
import re
from pathlib import Path

import httpx
import pytest

COLLECTION_PATH = Path(__file__).with_name("uw_assess.postman_collection.json")
DECISIONS = ("ACCEPT", "REFER", "DECLINE")
TIMEOUT_S = float(os.environ.get("UW_TEST_TIMEOUT_S", "300"))  # Ollama is slow

# Case 11's name states the letter of the guidelines (ACCEPT with a loading),
# but its description says REFER is equally defensible and is what the agent
# usually does: the system prompt names borderline crime as a REFER trigger.
# The case shows where the agent is more cautious than the rules, so either
# outcome passes.
BORDERLINE = {"11": {"ACCEPT", "REFER"}}


def _expected(case_id: str, name: str) -> set[str]:
    if case_id in BORDERLINE:
        return BORDERLINE[case_id]
    outcome = name.rsplit("—", 1)[-1]
    found = {d for d in DECISIONS if re.search(rf"\b{d}\b", outcome)}
    if not found:
        raise ValueError(f"No expected decision in case name: {name!r}")
    return found


def _case_id(name: str) -> str:
    """'02 Timber pre-1920 + High flood — ...' -> '02_timber_pre_1920_high_flood'"""
    title = name.split("—", 1)[0]
    return re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")


def _load_collection() -> tuple[str, list]:
    collection = json.loads(COLLECTION_PATH.read_text(encoding="utf-8"))
    base_url = next(
        (v["value"] for v in collection.get("variable", []) if v["key"] == "base_url"),
        "http://127.0.0.1:8010",
    )
    cases = []
    for item in collection["item"]:
        name = item["name"]
        submission = json.loads(item["request"]["body"]["raw"])
        expected = _expected(name.split()[0], name)
        cases.append(pytest.param(submission, expected, id=_case_id(name)))
    return base_url, cases


COLLECTION_BASE_URL, CASES = _load_collection()
BASE_URL = os.environ.get("UW_API_URL", COLLECTION_BASE_URL)


@pytest.fixture(scope="module")
def api():
    with httpx.Client(base_url=BASE_URL, timeout=TIMEOUT_S) as client:
        try:
            health = client.get("/health", timeout=5)
        except httpx.TransportError as e:
            pytest.skip(f"API not reachable at {BASE_URL} — start it with ./start.sh ({e})")
        status = health.json()
        if status.get("status") != "ok":
            pytest.fail(f"API is misconfigured: {status.get('detail')}")
        yield client


def _is_mock(result: dict) -> bool:
    try:
        return bool(json.loads(result.get("raw_agent_output") or "{}").get("mock_decision"))
    except (ValueError, AttributeError):
        return False


def test_collection_has_twelve_cases():
    assert len(CASES) == 12


@pytest.mark.parametrize("submission, expected", CASES)
def test_case_decision(api, submission, expected):
    resp = api.post("/assess", json=submission)
    assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text[:500]}"

    result = resp.json()
    if _is_mock(result):
        pytest.skip("API is running with MOCK_DECISION=true, so decisions are random")

    assert result["decision"] in expected, (
        f"got {result['decision']}, expected {' or '.join(sorted(expected))}\n"
        f"rationale: {result['rationale']}\n"
        f"risk_flags: {result['risk_flags']}\n"
        f"refer_reason: {result.get('refer_reason')}"
    )
