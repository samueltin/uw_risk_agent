"""
Risk Tools MCP Server v2
------------------------
Exposes all external risk lookup tools in one MCP server.

get_flood_zone      → Environment Agency flood-monitoring API (real, free)
get_crime_index     → data.police.uk street-level crime API (real, free)
get_claims_history  → mock (no free public API exists for this)
validate_submission → local logic, no external API

Dependencies:
    pip install fastmcp httpx

Run locally:
    python mcp_servers/risk_server_v2.py

The server starts at http://127.0.0.1:8001
Test with:
    python test_server.py
    fastmcp inspector mcp_servers/risk_server_v2.py
"""

import json
import httpx
from datetime import datetime, timedelta
from fastmcp import FastMCP

mcp = FastMCP("uw-risk-tools-v2")


# ---------------------------------------------------------------------------
# Shared helper: postcode → lat/lng via postcodes.io
# Free, no API key, no rate limit concern for low volume
# ---------------------------------------------------------------------------

async def _geocode(postcode: str) -> tuple[float, float]:
    """
    Convert a UK postcode to latitude/longitude using postcodes.io.
    Raises ValueError if the postcode is invalid or not found.
    """
    clean = postcode.strip().upper().replace(" ", "")
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"https://api.postcodes.io/postcodes/{clean}")

    if resp.status_code == 404:
        raise ValueError(f"Postcode '{postcode}' not found. Check it is a valid UK postcode.")
    resp.raise_for_status()

    result = resp.json().get("result")
    if not result:
        raise ValueError(f"No geocode result for postcode '{postcode}'.")

    return float(result["latitude"]), float(result["longitude"])


# ---------------------------------------------------------------------------
# Tool 1: get_flood_zone
# Source: Environment Agency flood-monitoring API
# Docs:   https://environment.data.gov.uk/flood-monitoring/doc/reference
# Auth:   None required
# Cost:   Free
#
# Note: The EA API returns active flood WARNINGS, not the static planning
# flood zone map (Zone 1/2/3). Active warnings are a strong signal of
# current risk and are the best free proxy for underwriting purposes.
# For production, use JBA Risk or Addresscloud for static zone data.
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_flood_zone(postcode: str) -> dict:
    """
    Returns flood risk data for a UK property postcode.
    Calls the Environment Agency real-time flood monitoring API.

    Returns active flood warning severity, Flood Re eligibility flag,
    and the number of active warnings within 5km of the property.

    Flood zone mapping from EA severity levels:
      Severity 1 (Severe warning) → Zone 3b
      Severity 2 (Flood warning)  → Zone 3a
      Severity 3 (Flood alert)    → Zone 2
      No active warnings          → Zone 1

    Important: this reflects ACTIVE warnings, not the static planning
    flood zone. A Zone 1 result means no current warnings, not zero
    long-term flood risk.
    """
    postcode_clean = postcode.strip().upper()

    try:
        lat, lng = await _geocode(postcode_clean)
    except ValueError as e:
        return {"error": str(e), "postcode": postcode_clean}

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            "https://environment.data.gov.uk/flood-monitoring/id/floods",
            params={"lat": lat, "long": lng, "dist": 5}
        )

    if resp.status_code != 200:
        return {
            "error": f"EA API returned HTTP {resp.status_code}",
            "postcode": postcode_clean,
            "flood_zone": "UNKNOWN",
            "data_source": "Environment Agency flood-monitoring API"
        }

    items = resp.json().get("items", [])

    # Find the highest severity warning (lowest number = most severe)
    severity_level = None
    warning_labels = []
    for item in items:
        level = item.get("severityLevel")
        label = item.get("description", item.get("eaAreaName", ""))
        if level is not None:
            warning_labels.append(f"Severity {level}: {label}")
            if severity_level is None or level < severity_level:
                severity_level = level

    # Map EA severity to insurance flood zone
    zone_map = {1: "Zone 3b", 2: "Zone 3a", 3: "Zone 2"}
    flood_zone = zone_map.get(severity_level, "Zone 1")
    flood_re_eligible = flood_zone in ("Zone 3a", "Zone 3b")

    return {
        "postcode": postcode_clean,
        "latitude": lat,
        "longitude": lng,
        "flood_zone": flood_zone,
        "ea_severity_level": severity_level,
        "active_warnings_within_5km": len(items),
        "warning_descriptions": warning_labels[:3],  # top 3 for brevity
        "flood_re_eligible": flood_re_eligible,
        "data_source": "Environment Agency flood-monitoring API",
        "note": (
            "Based on active EA flood warnings. "
            "Zone 1 = no active warnings within 5km, not zero long-term risk."
        )
    }


# ---------------------------------------------------------------------------
# Tool 2: get_crime_index
# Source: data.police.uk street-level crime API
# Docs:   https://data.police.uk/docs/method/crime-street/
# Auth:   None required
# Cost:   Free
#
# Fetches 3 months of street crime data and normalises to a 0-100 index.
# Property crime types (burglary, vehicle crime, theft) are weighted
# to reflect insurance-relevant risk signals.
# ---------------------------------------------------------------------------

PROPERTY_CRIME_CATEGORIES = {
    "burglary",
    "vehicle-crime",
    "theft-from-the-person",
    "robbery",
    "shoplifting",
    "criminal-damage-arson",
}


@mcp.tool()
async def get_crime_index(postcode: str) -> dict:
    """
    Returns property crime exposure index for a UK postcode.
    Calls the data.police.uk street-level crime API over the last 3 months.

    Index is 0-100 (higher = more property crime).
    Bands:
      LOW       (0–29)
      MEDIUM    (30–59)
      HIGH      (60–79)
      VERY_HIGH (80–100)

    Only counts property-relevant crime categories: burglary,
    vehicle crime, theft, robbery, shoplifting, criminal damage/arson.
    """
    postcode_clean = postcode.strip().upper()

    try:
        lat, lng = await _geocode(postcode_clean)
    except ValueError as e:
        return {"error": str(e), "postcode": postcode_clean}

    total_all_crimes = 0
    total_property_crimes = 0
    months_fetched = 0
    errors = []

    async with httpx.AsyncClient(timeout=20.0) as client:
        for months_back in range(1, 4):
            date = datetime.now() - timedelta(days=30 * months_back)
            month_str = date.strftime("%Y-%m")

            try:
                resp = await client.get(
                    "https://data.police.uk/api/crimes-street/all-crime",
                    params={"lat": lat, "lng": lng, "date": month_str}
                )

                if resp.status_code == 200:
                    crimes = resp.json()
                    total_all_crimes += len(crimes)
                    total_property_crimes += sum(
                        1 for c in crimes
                        if c.get("category") in PROPERTY_CRIME_CATEGORIES
                    )
                    months_fetched += 1
                elif resp.status_code == 503:
                    # Police API returns 503 when data isn't available for that month
                    errors.append(f"{month_str}: data not yet available")
                else:
                    errors.append(f"{month_str}: HTTP {resp.status_code}")

            except httpx.TimeoutException:
                errors.append(f"{month_str}: request timed out")

    if months_fetched == 0:
        return {
            "error": "Could not retrieve crime data — Police API unavailable.",
            "postcode": postcode_clean,
            "errors": errors,
            "data_source": "data.police.uk"
        }

    # Monthly average property crimes, normalised to 0-100 index
    monthly_avg = total_property_crimes / months_fetched
    index = round(min(monthly_avg * 5, 100), 1)

    if index < 30:
        band = "LOW"
    elif index < 60:
        band = "MEDIUM"
    elif index < 80:
        band = "HIGH"
    else:
        band = "VERY_HIGH"

    return {
        "postcode": postcode_clean,
        "latitude": lat,
        "longitude": lng,
        "crime_index": index,
        "crime_band": band,
        "property_crimes_total": total_property_crimes,
        "all_crimes_total": total_all_crimes,
        "months_analysed": months_fetched,
        "monthly_avg_property_crimes": round(monthly_avg, 1),
        "data_source": "data.police.uk street-level crime API",
        "errors": errors if errors else None
    }


# ---------------------------------------------------------------------------
# Tool 3: get_claims_history
# No free public API exists for insurance claims history in the UK.
# In production: LexisNexis C.L.U.E., CUE (Claims & Underwriting Exchange),
# or your insurer's internal claims system.
# ---------------------------------------------------------------------------

CLAIMS_DATA = {
    ("Jane Smith", "1978-06-15"):  {"verified_claims": 2, "types": ["escape_of_water", "subsidence"], "anomaly": False},
    ("John Brown", "1965-03-22"):  {"verified_claims": 0, "types": [], "anomaly": False},
    ("Alice Jones", "1990-11-01"): {"verified_claims": 1, "types": ["theft"], "anomaly": False},
    ("Robert Lee", "1955-07-14"):  {"verified_claims": 4, "types": ["flood", "flood", "escape_of_water", "fire"], "anomaly": True},
}


@mcp.tool()
def get_claims_history(applicant_name: str, date_of_birth: str) -> dict:
    """
    Retrieves verified prior claims history for an applicant.
    Cross-references declared claims against insurance industry database.
    Flags anomalies where declared claims do not match verified records.

    Note: uses mock data — no free public claims API exists in the UK.
    Production: integrate with CUE (Claims & Underwriting Exchange)
    or LexisNexis Risk Solutions.
    """
    key = (applicant_name.strip(), date_of_birth.strip())
    data = CLAIMS_DATA.get(key, {"verified_claims": 0, "types": [], "anomaly": False})

    return {
        "applicant_name": applicant_name,
        "verified_claims_count": data["verified_claims"],
        "claim_types": data["types"],
        "claims_anomaly_detected": data["anomaly"],
        "anomaly_note": (
            "Declared count does not match verified records"
            if data["anomaly"] else None
        ),
        "data_source": "Insurance Industry Claims Database (mock — CUE not publicly available)"
    }


# ---------------------------------------------------------------------------
# Tool 4: validate_submission
# ---------------------------------------------------------------------------

@mcp.tool()
def validate_submission(submission_json: str) -> dict:
    """
    Validates and normalises a broker submission for completeness
    and internal consistency. Checks for missing fields, implausible
    values, and calculates derived fields such as applicant age.
    Returns a list of validation flags if issues are found.
    """
    try:
        sub = json.loads(submission_json)
    except json.JSONDecodeError:
        return {"valid": False, "flags": ["INVALID_JSON"],
                "notes": "Could not parse submission JSON"}

    flags = []
    from datetime import date

    # Age check
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
    if sum_insured > 1_000_000:
        flags.append("SUM_INSURED_ABOVE_1M_REFER")

    return {
        "valid": "INVALID" not in " ".join(flags),
        "applicant_age": age,
        "flags": flags,
        "summary": (
            f"Applicant aged {age}, property built {year_built}, "
            f"sum insured £{sum_insured:,.0f}. "
            + (f"Flags: {', '.join(flags)}" if flags else "No validation issues.")
        )
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Starting UW Risk Tools MCP Server v2 on http://127.0.0.1:8001")
    print("Tools: get_flood_zone, get_crime_index, get_claims_history, validate_submission")
    print("Real APIs: Environment Agency (flood), data.police.uk (crime)")
    print("Press Ctrl+C to stop.\n")
    mcp.run(transport="streamable-http", host="127.0.0.1", port=8001)
