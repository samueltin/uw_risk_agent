"""
Underwriting Risk Assessment — FastAPI service
------------------------------------------------
Exposes the underwriting process over HTTP so the Streamlit UI (or any
other client) no longer imports an orchestrator directly.

    POST /findings   run the MCP risk tools only (no LLM)
    POST /assess     run a full assessment
    GET  /health     liveness plus the configured provider and model

Run:
    uvicorn api.main:app --port 8010 --reload

A single orchestrator (api/orchestrator.py) runs every assessment. Which
LLM drives it is fixed by UW_LLM_PROVIDER in .env — "azure" for gpt-4.1 or
"ollama" for a local model. It is a deployment decision: clients send
insurance data only and cannot select an engine.
"""

import os
import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .orchestrator import (
    collect_findings,
    LLM_PROVIDER,
    ProviderError,
    active_model,
    check_provider,
    run_underwriting_assessment,
)
from .schemas import (
    SubmissionRequest,
    DecisionResponse,
    FindingsResponse,
    HealthResponse,
)

logging.basicConfig(
    level=os.environ.get("UW_LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    force=True,          # configure_azure_monitor may already hold the root handler
)
logger = logging.getLogger(__name__)

def check_configuration() -> None:
    """
    Report the configured LLM provider at boot, not on the first assessment.

    A typo in UW_LLM_PROVIDER is otherwise invisible until a broker submits.
    Logs only — a misconfigured service still starts, and /assess returns 500.
    """
    try:
        check_provider()
    except ProviderError as e:
        logger.error(f"CONFIGURATION ERROR — {e}")
        return
    logger.info(f"LLM provider: '{LLM_PROVIDER}' | model: '{active_model()}'")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Startup and shutdown hooks. Nothing to tear down."""
    check_configuration()
    yield


app = FastAPI(
    title="Underwriting Risk Assessment API",
    description=(
        "Agentic property underwriting on Microsoft Agent Framework. The "
        "agent loop runs in-process against MCP risk tools and a RAG "
        "guidelines index, driven by either Azure gpt-4.1 or a local Ollama model."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health", response_model=HealthResponse, tags=["ops"])
def health() -> HealthResponse:
    """Liveness plus the configured LLM provider and model."""
    try:
        check_provider()
        configured = True
        detail = None
    except ProviderError as e:
        configured = False
        detail = str(e)

    return HealthResponse(
        status="ok" if configured else "misconfigured",
        provider=LLM_PROVIDER,
        model=active_model(),
        detail=detail,
    )


@app.post("/findings", response_model=FindingsResponse, tags=["underwriting"])
async def findings(request: SubmissionRequest) -> FindingsResponse:
    """
    Run the MCP risk tools and return their raw results — no LLM.

    This is the deterministic half of the assessment: flood zone, crime
    exposure, claims history and submission validation, exactly as the
    tools report them. Fast (a few seconds) compared with /assess.
    """
    submission = request.to_domain()
    logger.info(
        f"POST /findings | broker_ref={submission.broker_reference} | "
        f"postcode={submission.property_postcode}"
    )
    try:
        data = await collect_findings(submission)
    except Exception as e:
        logger.exception("Findings collection failed")
        raise HTTPException(
            status_code=502, detail=f"Risk tools unavailable: {e}"
        ) from e

    return FindingsResponse(**data)


@app.post("/assess", response_model=DecisionResponse, tags=["underwriting"])
async def assess(request: SubmissionRequest) -> DecisionResponse:
    """
    Run an underwriting assessment and return the decision.

    Expect this to take roughly 15-40 seconds: the agent calls MCP risk
    tools and the guidelines knowledge base in a loop before deciding.
    """
    try:
        check_provider()
    except ProviderError as e:
        # Server misconfiguration (bad UW_LLM_PROVIDER), not a client error.
        logger.error(str(e))
        raise HTTPException(status_code=500, detail=str(e)) from e

    submission = request.to_domain()
    logger.info(
        f"POST /assess | provider={LLM_PROVIDER} | model={active_model()} | "
        f"broker_ref={submission.broker_reference} | "
        f"postcode={submission.property_postcode}"
    )

    # run_underwriting_assessment is synchronous and blocks for tens of
    # seconds; it also calls asyncio.run() internally, which would fail on
    # the event-loop thread. The threadpool hop is required, not cosmetic.
    try:
        decision = await run_in_threadpool(run_underwriting_assessment, submission)
    except Exception as e:
        # The assessment itself failed (MCP unreachable, model error, ...).
        logger.exception("Assessment failed")
        raise HTTPException(
            status_code=502, detail=f"Assessment failed: {e}"
        ) from e

    return DecisionResponse.from_domain(decision, LLM_PROVIDER, active_model())


@app.exception_handler(ProviderError)
async def provider_error_handler(request, exc: ProviderError) -> JSONResponse:
    """A misconfigured UW_LLM_PROVIDER is a server fault, not a bad request."""
    return JSONResponse(status_code=500, content={"detail": str(exc)})
