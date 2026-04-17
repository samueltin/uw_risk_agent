"""
monitor/telemetry.py
--------------------
Azure-native LLM telemetry using OpenTelemetry + Azure Monitor.

Tracks the same signals as Prometheus counters but with zero
infrastructure overhead — all metrics flow to Azure Monitor
(Application Insights) as custom metrics, queryable via KQL
in Log Analytics.

Metrics tracked:
  llm_requests_total         — counter, by model and status
  llm_errors_total           — counter, by model
  llm_prompt_tokens_total    — counter, by model
  llm_completion_tokens_total — counter, by model
  llm_estimated_cost_usd     — counter, by model
  llm_request_duration_ms    — histogram, by model
"""

from opentelemetry import metrics, trace

# ---------------------------------------------------------------------------
# Tracer — used in orchestrator.py to create named spans
# ---------------------------------------------------------------------------

tracer = trace.get_tracer("uw_risk_agent")

# ---------------------------------------------------------------------------
# Meter + instruments
# ---------------------------------------------------------------------------

_meter = metrics.get_meter("uw_risk_agent")

_request_counter = _meter.create_counter(
    name="llm_requests_total",
    description="Total number of LLM agent runs",
    unit="1",
)

_error_counter = _meter.create_counter(
    name="llm_errors_total",
    description="Total number of failed LLM agent runs",
    unit="1",
)

_prompt_token_counter = _meter.create_counter(
    name="llm_prompt_tokens_total",
    description="Cumulative prompt tokens consumed",
    unit="1",
)

_completion_token_counter = _meter.create_counter(
    name="llm_completion_tokens_total",
    description="Cumulative completion tokens generated",
    unit="1",
)

_cost_counter = _meter.create_counter(
    name="llm_estimated_cost_usd",
    description="Estimated LLM cost in USD (approximate)",
    unit="USD",
)

_latency_histogram = _meter.create_histogram(
    name="llm_request_duration_ms",
    description="LLM agent run latency in milliseconds",
    unit="ms",
)

# ---------------------------------------------------------------------------
# GPT-4.1 pricing (per 1K tokens, adjust if model changes)
# https://azure.microsoft.com/en-us/pricing/details/cognitive-services/openai-service/
# ---------------------------------------------------------------------------

_PROMPT_COST_PER_1K = 0.002
_COMPLETION_COST_PER_1K = 0.008


# ---------------------------------------------------------------------------
# Public tracking function
# ---------------------------------------------------------------------------

def track_llm_call(
    *,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: float,
    success: bool = True,
    broker_reference: str | None = None,
) -> None:
    """
    Emit all LLM telemetry signals for a single agent run.

    Call this once after agents_client.runs.create_and_process() returns.

    Parameters
    ----------
    model               Azure OpenAI model deployment name (e.g. "gpt-4.1")
    prompt_tokens       Tokens consumed from run.usage.prompt_tokens
    completion_tokens   Tokens generated from run.usage.completion_tokens
    latency_ms          Wall-clock time for the full agent run in ms
    success             False if the run status was "failed"
    broker_reference    Optional — adds dimension for per-case filtering in KQL
    """
    attributes: dict[str, str] = {"model": model}
    if broker_reference:
        attributes["broker_reference"] = broker_reference

    # Estimated cost
    cost = (
        (prompt_tokens / 1000 * _PROMPT_COST_PER_1K)
        + (completion_tokens / 1000 * _COMPLETION_COST_PER_1K)
    )

    _request_counter.add(1, attributes)
    _prompt_token_counter.add(prompt_tokens, attributes)
    _completion_token_counter.add(completion_tokens, attributes)
    _cost_counter.add(cost, attributes)
    _latency_histogram.record(latency_ms, attributes)

    if not success:
        _error_counter.add(1, attributes)
