"""
Pydantic request/response schemas for the underwriting API.

models/ holds plain dataclasses shared by the orchestrators. These schemas
are the HTTP boundary: they validate incoming JSON, convert to and from the
dataclasses, and give FastAPI an OpenAPI schema to publish.
"""

from typing import Optional
from pydantic import BaseModel, ConfigDict, Field

from models.submission import UnderwritingSubmission
from models.decision import UnderwritingDecision


class SubmissionRequest(BaseModel):
    """A broker submission.

    Carries insurance data only. Which orchestrator runs the assessment is
    a deployment decision (UW_LLM_PROVIDER), not something a client may choose.
    """

    # Reject unknown keys rather than ignoring them, so a client sending a
    # stale field is told plainly instead of being silently ignored.
    model_config = ConfigDict(extra="forbid")

    # Applicant
    applicant_name: str = Field(..., examples=["Jane Smith"])
    date_of_birth: str = Field(..., examples=["1978-06-15"], description="ISO 8601")
    occupation: str = Field(..., examples=["Teacher"])

    # Risk location
    property_address: str = Field(..., examples=["12 Riverside Close, Bristol"])
    property_postcode: str = Field(..., examples=["BS1 4DJ"])
    property_type: str = Field(..., examples=["detached"])
    year_built: int = Field(..., ge=1600, le=2100, examples=[1912])
    construction: str = Field(..., examples=["timber"])
    num_storeys: int = Field(..., ge=1, le=100, examples=[2])

    # Coverage
    product_type: str = Field(..., examples=["combined"])
    sum_insured: float = Field(..., gt=0, examples=[425000.0])
    policy_start_date: str = Field(..., examples=["2026-05-01"], description="ISO 8601")

    # Loss history
    claims_last_5_years: int = Field(..., ge=0, examples=[2])
    prior_claim_types: list[str] = Field(default_factory=list)
    outstanding_claims: bool = False

    # Optional
    broker_reference: Optional[str] = None
    special_conditions: Optional[str] = None

    def to_domain(self) -> UnderwritingSubmission:
        """Convert to the dataclass the orchestrators expect."""
        return UnderwritingSubmission(**self.model_dump())


class DecisionResponse(BaseModel):
    """The underwriting decision, plus which LLM produced it."""

    decision: str
    confidence: str
    rationale: str
    risk_flags: list[str]
    flood_re_eligible: bool
    refer_reason: Optional[str] = None
    recommended_premium_loading: Optional[float] = None
    broker_reference: Optional[str] = None
    raw_agent_output: str = ""
    processing_time_ms: Optional[int] = None
    provider: str
    model: str

    @classmethod
    def from_domain(
        cls, decision: UnderwritingDecision, provider: str, model: str
    ) -> "DecisionResponse":
        return cls(
            decision=decision.decision.value,
            confidence=decision.confidence,
            rationale=decision.rationale,
            risk_flags=[str(f) for f in decision.risk_flags],
            flood_re_eligible=decision.flood_re_eligible,
            refer_reason=decision.refer_reason,
            recommended_premium_loading=decision.recommended_premium_loading,
            broker_reference=decision.broker_reference,
            raw_agent_output=decision.raw_agent_output,
            processing_time_ms=decision.processing_time_ms,
            provider=provider,
            model=model,
        )


class FindingsResponse(BaseModel):
    """
    Raw output of the MCP risk tools, before any interpretation.

    Each field is whatever the tool returned, unchanged — the point is to
    show the calibrated data a conventional system can produce on its own.
    """

    flood: dict = Field(default_factory=dict)
    crime: dict = Field(default_factory=dict)
    claims: dict = Field(default_factory=dict)
    validation: dict = Field(default_factory=dict)


class HealthResponse(BaseModel):
    status: str
    provider: str          # "azure" | "ollama"
    model: str
    detail: Optional[str] = None
