from dataclasses import dataclass, field
from typing import Optional
import json


@dataclass
class UnderwritingSubmission:
    # Applicant
    applicant_name: str
    date_of_birth: str          # ISO 8601  e.g. "1975-04-12"
    occupation: str

    # Risk location
    property_address: str
    property_postcode: str
    property_type: str          # "detached" | "semi" | "flat" | "commercial"
    year_built: int
    construction: str           # "brick" | "timber" | "concrete"
    num_storeys: int

    # Coverage
    product_type: str           # "buildings" | "contents" | "combined"
    sum_insured: float          # GBP
    policy_start_date: str      # ISO 8601

    # Loss history
    claims_last_5_years: int
    prior_claim_types: list     = field(default_factory=list)
    outstanding_claims: bool    = False

    # Optional
    broker_reference: Optional[str] = None
    special_conditions: Optional[str] = None

    def to_prompt_str(self) -> str:
        return (
            f"Applicant: {self.applicant_name}, DOB: {self.date_of_birth}, "
            f"Occupation: {self.occupation}\n"
            f"Property: {self.property_address} ({self.property_postcode}), "
            f"Type: {self.property_type}, Built: {self.year_built}, "
            f"Construction: {self.construction}, Storeys: {self.num_storeys}\n"
            f"Coverage: {self.product_type}, Sum insured: £{self.sum_insured:,.0f}, "
            f"Start: {self.policy_start_date}\n"
            f"Claims (5yr): {self.claims_last_5_years}, "
            f"Types: {', '.join(self.prior_claim_types) or 'None'}, "
            f"Outstanding: {self.outstanding_claims}\n"
            + (f"Special conditions: {self.special_conditions}\n"
               if self.special_conditions else "")
        )

    def to_json(self) -> str:
        return json.dumps({
            "applicant_name": self.applicant_name,
            "date_of_birth": self.date_of_birth,
            "occupation": self.occupation,
            "property_address": self.property_address,
            "property_postcode": self.property_postcode,
            "property_type": self.property_type,
            "year_built": self.year_built,
            "construction": self.construction,
            "num_storeys": self.num_storeys,
            "product_type": self.product_type,
            "sum_insured": self.sum_insured,
            "policy_start_date": self.policy_start_date,
            "claims_last_5_years": self.claims_last_5_years,
            "prior_claim_types": self.prior_claim_types,
            "outstanding_claims": self.outstanding_claims,
            "broker_reference": self.broker_reference,
            "special_conditions": self.special_conditions,
        }, indent=2)
