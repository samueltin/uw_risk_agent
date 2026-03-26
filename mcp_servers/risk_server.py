"""
Risk Tools MCP Server
---------------------
Exposes all external risk lookup tools in one MCP server.
The LLM agent decides which tools to call and in what order —
this server just makes them available over the MCP protocol.

Tools exposed:
  - get_flood_zone(postcode)
  - get_crime_index(postcode)
  - get_claims_history(applicant_name, date_of_birth)
  - validate_submission(submission_json)

Run locally:
  python mcp_servers/risk_server.py

In production:
  Deploy as an Azure Container App and set MCP_RISK_SERVER_URL
  in your environment to point agents at the SSE endpoint.
"""

import json
from fastmcp import FastMCP

mcp = FastMCP("uw-risk-tools")


# ---------------------------------------------------------------------------
# Mock data — replace with real API calls:
#   Flood:  Environment Agency / JBA Risk
#   Crime:  ONS Crime Statistics API
#   Claims: LexisNexis C.L.U.E. / internal claims system
# ---------------------------------------------------------------------------

FLOOD_DATA = {
    "SW1A 1AA": {"zone": "Zone 1", "river_depth": 0.0,  "surface_depth": 0.05, "coastal": False, "flood_re": False},
    "BS1 4DJ":  {"zone": "Zone 3a","river_depth": 0.8,  "surface_depth": 0.45, "coastal": False, "flood_re": True},
    "TQ9 5EJ":  {"zone": "Zone 3b","river_depth": 1.2,  "surface_depth": 0.90, "coastal": True,  "flood_re": True},
    "EX2 5AE":  {"zone": "Zone 2", "river_depth": 0.3,  "surface_depth": 0.20, "coastal": False, "flood_re": False},
    "YO1 9WT":  {"zone": "Zone 3a","river_depth": 0.65, "surface_depth": 0.35, "coastal": False, "flood_re": True},
}

CRIME_DATA = {
    "SW1A 1AA": {"index": 42.1, "band": "MEDIUM"},
    "BS1 4DJ":  {"index": 78.4, "band": "HIGH"},
    "TQ9 5EJ":  {"index": 18.2, "band": "LOW"},
    "EX2 5AE":  {"index": 34.7, "band": "MEDIUM"},
    "YO1 9WT":  {"index": 55.3, "band": "MEDIUM"},
}

CLAIMS_DATA = {
    ("Jane Smith", "1978-06-15"):   {"verified_claims": 2, "types": ["escape_of_water", "subsidence"], "anomaly": False},
    ("John Brown", "1965-03-22"):   {"verified_claims": 0, "types": [], "anomaly": False},
    ("Alice Jones", "1990-11-01"):  {"verified_claims": 1, "types": ["theft"], "anomaly": False},
    ("Robert Lee",  "1955-07-14"):  {"verified_claims": 4, "types": ["flood", "flood", "escape_of_water", "fire"], "anomaly": True},
}


# ---------------------------------------------------------------------------
# Tool definitions — the LLM sees these as callable functions
# ---------------------------------------------------------------------------

@mcp.tool()
def get_flood_zone(postcode: str) -> dict:
    """
    Returns flood risk data for a UK property by postcode.
    Uses Environment Agency flood zone classifications:
      Zone 1  = low probability  (<0.1% annual chance)
      Zone 2  = medium           (0.1–1%)
      Zone 3a = high             (>1%)
      Zone 3b = functional floodplain (highest risk)
    Also returns Flood Re eligibility for UK underwriting decisions.
    """
    key = postcode.strip().upper()
    data = FLOOD_DATA.get(key, {"zone": "Zone 1", "river_depth": 0.0,
                                 "surface_depth": 0.02, "coastal": False, "flood_re": False})
    return {
        "postcode": key,
        "flood_zone": data["zone"],
        "river_flood_depth_1in75_metres": data["river_depth"],
        "surface_water_depth_1in75_metres": data["surface_depth"],
        "coastal_risk": data["coastal"],
        "flood_re_eligible": data["flood_re"],
        "data_source": "Environment Agency (mock)"
    }


@mcp.tool()
def get_crime_index(postcode: str) -> dict:
    """
    Returns property crime exposure index for a UK postcode.
    Index is 0–100 (higher = more crime).
    Bands: LOW (<30), MEDIUM (30–60), HIGH (60–80), VERY_HIGH (>80).
    Based on ONS Crime Statistics methodology.
    """
    key = postcode.strip().upper()
    data = CRIME_DATA.get(key, {"index": 25.0, "band": "LOW"})
    return {
        "postcode": key,
        "crime_index": data["index"],
        "crime_band": data["band"],
        "data_source": "ONS Crime Statistics (mock)"
    }


@mcp.tool()
def get_claims_history(applicant_name: str, date_of_birth: str) -> dict:
    """
    Retrieves verified prior claims history for an applicant.
    Cross-references declared claims against insurance industry database.
    Flags anomalies where declared claims don't match verified records.
    """
    key = (applicant_name.strip(), date_of_birth.strip())
    data = CLAIMS_DATA.get(key, {"verified_claims": 0, "types": [], "anomaly": False})
    return {
        "applicant_name": applicant_name,
        "verified_claims_count": data["verified_claims"],
        "claim_types": data["types"],
        "claims_anomaly_detected": data["anomaly"],
        "anomaly_note": "Declared count does not match verified records" if data["anomaly"] else None,
        "data_source": "Insurance Industry Claims Database (mock)"
    }


@mcp.tool()
def validate_submission(submission_json: str) -> dict:
    """
    Validates and normalises a broker submission for completeness
    and internal consistency. Checks for missing fields, implausible
    values, and calculates derived fields (e.g. applicant age).
    Returns a list of validation flags if issues are found.
    """
    try:
        sub = json.loads(submission_json)
    except json.JSONDecodeError:
        return {"valid": False, "flags": ["INVALID_JSON"], "notes": "Could not parse submission JSON"}

    flags = []
    notes = []

    # Age check
    from datetime import date
    try:
        dob = date.fromisoformat(sub.get("date_of_birth", ""))
        age = (date.today() - dob).days // 365
        if age < 18:
            flags.append("APPLICANT_UNDER_18")
        if age > 85:
            flags.append("APPLICANT_OVER_85")
    except ValueError:
        flags.append("INVALID_DOB_FORMAT")
        age = None

    # Property checks
    year_built = sub.get("year_built", 0)
    if year_built < 1700:
        flags.append("UNUSUALLY_OLD_PROPERTY")
    if year_built > date.today().year:
        flags.append("FUTURE_BUILD_DATE")

    # Sum insured
    sum_insured = sub.get("sum_insured", 0)
    if sum_insured < 50000:
        flags.append("SUM_INSURED_VERY_LOW")
    if sum_insured > 5000000:
        flags.append("SUM_INSURED_ABOVE_5M_REFER")

    # High-risk construction
    if sub.get("construction") == "timber" and year_built < 1920:
        flags.append("TIMBER_PRE_1920_HIGH_RISK")

    # Claims
    if sub.get("claims_last_5_years", 0) >= 3:
        flags.append("THREE_OR_MORE_CLAIMS")
    if sub.get("outstanding_claims"):
        flags.append("OUTSTANDING_CLAIMS_PRESENT")

    return {
        "valid": len([f for f in flags if "INVALID" in f]) == 0,
        "applicant_age": age,
        "flags": flags,
        "notes": notes,
        "summary": (
            f"Applicant aged {age}, property built {year_built}, "
            f"sum insured £{sum_insured:,.0f}. "
            + (f"Flags: {', '.join(flags)}" if flags else "No validation issues found.")
        )
    }


if __name__ == "__main__":
    # stdio for local dev/testing; use "sse" for cloud deployment
    # mcp.run(transport="stdio")
    # For local testing: use streamable-http so you can call it from a browser/inspector
    mcp.run(transport="streamable-http", host="127.0.0.1", port=8001)
