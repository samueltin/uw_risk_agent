from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


class Decision(str, Enum):
    ACCEPT  = "ACCEPT"
    REFER   = "REFER"
    DECLINE = "DECLINE"


@dataclass
class UnderwritingDecision:
    decision: Decision
    confidence: str                       # "HIGH" | "MEDIUM" | "LOW"
    rationale: str
    risk_flags: list
    flood_re_eligible: bool
    refer_reason: Optional[str]
    recommended_premium_loading: Optional[float]
    broker_reference: Optional[str]
    raw_agent_output: str                 # full LLM response for audit
    processing_time_ms: Optional[int] = None
