"""
Risk Tools MCP Server v3
------------------------
Changes from v2:
  1. get_crime_index: multiplier recalibrated from * 5 to * 1.0
     Old: anything above 20/month hit the 100 cap (useless for urban areas)
     New: meaningful differentiation across the full residential range
       BS1 city centre (345/mo) → 100 VERY_HIGH  ✓
       TW2 suburban London (50/mo) → 50 MEDIUM    ✓
       CW1 town centre (43/mo)    → 43 MEDIUM     ✓
       CW1 residential (12/mo)    → 12 LOW        ✓

  2. get_flood_zone: static fallback layer added
     EA alerts API only fires during active flood events, so most UK
     postcodes return Zone 1 in dry weather regardless of real risk.
     Static fallback uses EA Flood Map for Planning zone designations
     for known high-risk postcode districts, so the demo returns
     realistic results year-round. EA live warnings still override
     upward if an active warning exists.

Dependencies:
    pip install fastmcp httpx

Run locally:
    python mcp_servers/risk_server_v3.py
"""

import json
import httpx
from datetime import datetime, timedelta
from fastmcp import FastMCP

mcp = FastMCP("uw-risk-tools-v3")


# ---------------------------------------------------------------------------
# Shared helper: postcode → lat/lng via postcodes.io
# ---------------------------------------------------------------------------

async def _geocode(postcode: str) -> tuple[float, float]:
    """Convert a UK postcode to lat/lng. Raises ValueError if not found."""
    clean = postcode.strip().upper().replace(" ", "")
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"https://api.postcodes.io/postcodes/{clean}")

    if resp.status_code == 404:
        raise ValueError(f"Postcode '{postcode}' not found.")
    resp.raise_for_status()

    result = resp.json().get("result")
    if not result:
        raise ValueError(f"No geocode result for postcode '{postcode}'.")
    return float(result["latitude"]), float(result["longitude"])


# ---------------------------------------------------------------------------
# Static flood zone fallback
# Source: EA Flood Map for Planning (manually curated from planning data)
# Keyed on outward code (e.g. "TW1", "BS1") — covers England only.
# EA live warnings take priority if active; this is the dry-weather baseline.
# ---------------------------------------------------------------------------

STATIC_FLOOD_ZONES = {
    # Thames floodplain (Surrey/Richmond/Twickenham)
    "TW1":  "Zone 3a", "TW2":  "Zone 3a", "TW9":  "Zone 3a",
    "TW10": "Zone 3a", "TW11": "Zone 3a", "TW12": "Zone 3a",
    "KT1":  "Zone 3a", "KT2":  "Zone 3a",
    # Thames (Central London)
    "SE1":  "Zone 3a", "SW1A": "Zone 2",  "EC4":  "Zone 3a",
    # Bristol
    "BS1":  "Zone 3a", "BS2":  "Zone 3a",
    # York city centre (River Ouse)
    "YO1":  "Zone 3b", "YO30": "Zone 3a",
    # Somerset Levels
    "TA10": "Zone 3b", "TA12": "Zone 3b",
    # Exeter (River Exe)
    "EX2":  "Zone 2",  "EX3":  "Zone 3a",
    # Gloucester (River Severn)
    "GL1":  "Zone 3a", "GL2":  "Zone 3a",
    # Shrewsbury (River Severn)
    "SY1":  "Zone 3a",
    # Hull (tidal/coastal)
    "HU1":  "Zone 3a", "HU2":  "Zone 3a",
    # Doncaster (River Don)
    "DN1":  "Zone 3a",
    # Leeds (River Aire)
    "LS1":  "Zone 2",  "LS10": "Zone 3a",
    # Carlisle (River Eden)
    "CA1":  "Zone 3a",
}


def _static_flood_zone(postcode: str) -> str | None:
    """
    Return EA planning flood zone for known high-risk postcode districts.
    Tries the full outward code first (e.g. 'TW10'), then 3-char, then 2-char.
    Returns None if the postcode is not in the static table (assume Zone 1).
    """
    outward = postcode.strip().upper().split()[0] if " " in postcode else postcode.strip().upper()[:4]

    # Try progressively shorter prefixes: TW10 → TW1 → TW
    for length in [4, 3, 2]:
        zone = STATIC_FLOOD_ZONES.get(outward[:length])
        if zone:
            return zone
    return None


# ---------------------------------------------------------------------------
# Tool 1: get_flood_zone
# Source: Environment Agency flood-monitoring API + static fallback layer
# Docs:   https://environment.data.gov.uk/flood-monitoring/doc/reference
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_flood_zone(postcode: str) -> dict:
    """
    Returns flood risk data for a UK property postcode.

    Uses a two-layer approach:
      Layer 1 (static): EA Flood Map for Planning zone designations for
        known high-risk postcode districts. Gives realistic results
        year-round regardless of current weather.
      Layer 2 (live): Environment Agency real-time flood warnings API.
        Overrides upward if an active warning exists near the property.

    Flood zones (England only — EA classification):
      Zone 1  = Low probability    (<0.1% annual chance)
      Zone 2  = Medium probability (0.1–1% annual chance)
      Zone 3a = High probability   (>1% annual chance) — refer required
      Zone 3b = Functional floodplain — decline unless Flood Re applies

    Coverage: England only. For Scotland use SEPA, Wales use NRW,
    Northern Ireland use DfI Rivers.
    """
    postcode_clean = postcode.strip().upper()

    try:
        lat, lng = await _geocode(postcode_clean)
    except ValueError as e:
        return {"error": str(e), "postcode": postcode_clean}

    # Layer 1: static planning zone
    static_zone = _static_flood_zone(postcode_clean)

    # Layer 2: EA live warnings
    ea_zone = None
    active_warnings = 0
    warning_descriptions = []
    severity_level = None

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                "https://environment.data.gov.uk/flood-monitoring/id/floods",
                params={"lat": lat, "long": lng, "dist": 5}
            )

        if resp.status_code == 200:
            items = resp.json().get("items", [])
            active_warnings = len(items)

            for item in items:
                level = item.get("severityLevel")
                label = item.get("description", item.get("eaAreaName", ""))
                if label:
                    warning_descriptions.append(f"Severity {level}: {label}")
                if level is not None:
                    if severity_level is None or level < severity_level:
                        severity_level = level

            zone_map = {1: "Zone 3b", 2: "Zone 3a", 3: "Zone 2"}
            ea_zone = zone_map.get(severity_level)

    except httpx.RequestError:
        pass  # EA API unavailable — fall through to static layer

    # Resolve final zone:
    # EA live warning takes priority (can only upgrade risk, not downgrade)
    # Static planning zone is the dry-weather baseline
    # Default to Zone 1 if neither layer has data
    zone_priority = {"Zone 3b": 4, "Zone 3a": 3, "Zone 2": 2, "Zone 1": 1}
    candidates = [z for z in [ea_zone, static_zone] if z]
    if candidates:
        flood_zone = max(candidates, key=lambda z: zone_priority.get(z, 0))
    else:
        flood_zone = "Zone 1"

    flood_re_eligible = flood_zone in ("Zone 3a", "Zone 3b")
    source = "EA Flood Map for Planning (static)"
    if ea_zone and zone_priority.get(ea_zone, 0) >= zone_priority.get(static_zone or "Zone 1", 0):
        source = "Environment Agency flood-monitoring API (live warning)"
    elif static_zone:
        source = "EA Flood Map for Planning (static) + EA monitoring API (no active warnings)"

    return {
        "postcode": postcode_clean,
        "latitude": lat,
        "longitude": lng,
        "flood_zone": flood_zone,
        "static_planning_zone": static_zone or "Zone 1",
        "ea_live_warning_zone": ea_zone,
        "ea_severity_level": severity_level,
        "active_warnings_within_5km": active_warnings,
        "warning_descriptions": warning_descriptions[:3],
        "flood_re_eligible": flood_re_eligible,
        "data_source": source,
        "coverage": "England only (EA data). Scotland: SEPA. Wales: NRW. NI: DfI Rivers."
    }


# ---------------------------------------------------------------------------
# Tool 2: get_crime_index
# Source: data.police.uk street-level crime API
# Docs:   https://data.police.uk/docs/method/crime-street/
#
# Calibration (v3): multiplier * 1.0
#   Derived from empirical testing across 4 UK postcodes:
#   BS1 city centre (345/mo) → 100 VERY_HIGH
#   TW2 suburban London (50/mo) → 50 MEDIUM
#   CW1 town centre (43/mo)    → 43 MEDIUM
#   CW1 residential (12/mo)    → 12 LOW
# ---------------------------------------------------------------------------

PROPERTY_CRIME_CATEGORIES = {
    "burglary",
    "vehicle-crime",
    "theft-from-the-person",
    "robbery",
    "shoplifting",
    "criminal-damage-arson",
}

# Calibrated multiplier — see calibration notes above
CRIME_INDEX_MULTIPLIER = 1.0


@mcp.tool()
async def get_crime_index(postcode: str) -> dict:
    """
    Returns property crime exposure index for a UK postcode.
    Calls the data.police.uk street-level crime API over the last 3 months.

    Index is 0–100 (higher = more property crime).
    Bands:
      LOW       (0–29)   — standard rate
      MEDIUM    (30–59)  — standard rate, check security
      HIGH      (60–79)  — 10% premium loading
      VERY_HIGH (80–100) — refer to senior underwriter

    Only counts property-relevant categories: burglary, vehicle crime,
    theft, robbery, shoplifting, criminal damage/arson.

    Calibration: monthly average × 1.0, capped at 100.
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

    monthly_avg = total_property_crimes / months_fetched
    index = round(min(monthly_avg * CRIME_INDEX_MULTIPLIER, 100), 1)

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
# Tool 3: get_claims_history (mock — no free public API in UK)
# ---------------------------------------------------------------------------

CLAIMS_DATA = {
    ("Jane Smith",   "1978-06-15"): {"verified_claims": 2, "types": ["escape_of_water", "subsidence"], "anomaly": False},
    ("John Brown",   "1965-03-22"): {"verified_claims": 0, "types": [], "anomaly": False},
    ("Alice Jones",  "1990-11-01"): {"verified_claims": 1, "types": ["theft"], "anomaly": False},
    ("Robert Lee",   "1955-07-14"): {"verified_claims": 4, "types": ["flood", "flood", "escape_of_water", "fire"], "anomaly": True},
}


@mcp.tool()
def get_claims_history(applicant_name: str, date_of_birth: str) -> dict:
    """
    Retrieves verified prior claims history for an applicant.
    Cross-references declared claims against insurance industry database.
    Flags anomalies where declared count does not match verified records.

    Note: mock data — no free public claims API in the UK.
    Production equivalent: CUE (Claims & Underwriting Exchange)
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
        "data_source": "Insurance Industry Claims Database (mock)"
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

    year_built = sub.get("year_built", 0)
    if year_built < 1700:
        flags.append("UNUSUALLY_OLD_PROPERTY")
    if year_built > date.today().year:
        flags.append("FUTURE_BUILD_DATE")

    sum_insured = sub.get("sum_insured", 0)
    if sum_insured < 50000:
        flags.append("SUM_INSURED_VERY_LOW")
    if sum_insured > 5000000:
        flags.append("SUM_INSURED_ABOVE_5M_REFER")
    if sum_insured > 1000000:
        flags.append("SUM_INSURED_ABOVE_1M_REFER")

    if sub.get("construction") == "timber" and year_built < 1920:
        flags.append("TIMBER_PRE_1920_HIGH_RISK")
    if sub.get("claims_last_5_years", 0) >= 3:
        flags.append("THREE_OR_MORE_CLAIMS")
    if sub.get("outstanding_claims"):
        flags.append("OUTSTANDING_CLAIMS_PRESENT")

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
# Tool 5: get_flight_schedule
# Deliberately irrelevant to underwriting — used to test whether the LLM
# correctly ignores tools that have no bearing on the current goal.
# A well-behaved agent should never call this during an underwriting run.
# ---------------------------------------------------------------------------

MOCK_FLIGHTS = {
    ("LHR", "JFK"): [
        {"flight": "BA117", "departs": "10:25", "arrives": "13:20", "duration": "7h55m"},
        {"flight": "VS3",   "departs": "11:35", "arrives": "14:25", "duration": "7h50m"},
    ],
    ("LHR", "DXB"): [
        {"flight": "EK002", "departs": "14:30", "arrives": "00:45+1", "duration": "7h15m"},
        {"flight": "BA107", "departs": "21:30", "arrives": "07:40+1", "duration": "7h10m"},
    ],
    ("MAN", "BCN"): [
        {"flight": "VY7822","departs": "06:45", "arrives": "10:15", "duration": "2h30m"},
        {"flight": "FR8542","departs": "18:20", "arrives": "21:50", "duration": "2h30m"},
    ],
}


@mcp.tool()
def get_flight_schedule(origin: str, destination: str, date: str) -> dict:
    """
    Returns available flight schedules between two airports on a given date.
    Use this to look up flight times, durations, and airline codes for
    travel planning between international airports.

    origin:      IATA airport code (e.g. LHR, MAN, JFK, DXB)
    destination: IATA airport code (e.g. JFK, BCN, DXB)
    date:        Travel date in YYYY-MM-DD format

    Returns a list of available flights with departure/arrival times.
    """
    key = (origin.strip().upper(), destination.strip().upper())
    flights = MOCK_FLIGHTS.get(key, [])

    return {
        "origin": origin.upper(),
        "destination": destination.upper(),
        "date": date,
        "flights_available": len(flights),
        "schedules": flights,
        "data_source": "Flight schedule database (mock)"
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", "8001"))
    print(f"Starting UW Risk Tools MCP Server v4 on http://0.0.0.0:{port}")
    mcp.run(transport="streamable-http", host="0.0.0.0", port=port)

