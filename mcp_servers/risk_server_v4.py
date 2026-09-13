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

# National baseline for property crimes per month within the ~1 mile radius
# the police API returns. Derived with the same method as this tool: median
# across 10 sampled postcodes (city centre / town / suburb / rural),
# 2026-05 data. Used to express exposure as a multiple of the average,
# which reads more plainly than a 0-100 index.
NATIONAL_AVG_MONTHLY_PROPERTY_CRIMES = 200

# Some forces (notably Greater Manchester) no longer supply data to
# data.police.uk. The street-level API still answers HTTP 200 with an empty
# list, which is indistinguishable from a genuinely quiet rural area — so a
# crime count alone cannot tell them apart. Instead we ask which force
# covers the point, then check whether that force published anything at all
# that month. A false LOW would let a high-crime city centre through at
# standard rates, so this matters.
_FORCE_PUBLISHES_CACHE: dict[str, bool] = {}

# Below this monthly average we verify the force actually publishes before
# reporting a LOW band. Genuinely quiet areas pass the check and stay LOW.
CRIME_QUIET_THRESHOLD = 20


async def _locate_force(client, lat: float, lng: float) -> str | None:
    """Which police force covers these coordinates."""
    try:
        resp = await client.get(
            "https://data.police.uk/api/locate-neighbourhood",
            params={"q": f"{lat},{lng}"},
        )
        if resp.status_code == 200:
            return resp.json().get("force")
    except httpx.HTTPError:
        pass
    return None


async def _force_publishes(client, force: str) -> bool:
    """
    Whether this force supplies crime data at all.

    Checks several recent months: the latest one or two are often not yet
    published for ANY force, so a single empty month proves nothing. The
    force counts as publishing if any checked month has data.
    """
    if force in _FORCE_PUBLISHES_CACHE:
        return _FORCE_PUBLISHES_CACHE[force]

    publishes = False
    for months_back in range(2, 6):
        date = datetime.now() - timedelta(days=30 * months_back)
        try:
            resp = await client.get(
                "https://data.police.uk/api/crimes-no-location",
                params={
                    "category": "all-crime",
                    "force": force,
                    "date": date.strftime("%Y-%m"),
                },
            )
            if resp.status_code == 200 and len(resp.json()) > 0:
                publishes = True
                break
        except httpx.HTTPError:
            # Network trouble is not evidence of non-publication; assume it
            # publishes so a transient failure cannot mark a real area LOW.
            publishes = True
            break

    _FORCE_PUBLISHES_CACHE[force] = publishes
    return publishes


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

    Calibration: monthly average × 1.0, capped at 100. The index saturates
    for any urban area, so prefer vs_national_average and crime_summary when
    explaining the result to a person — the index only drives the band.

    Returns crime_band "DATA_UNAVAILABLE" when the covering force does not
    publish street-level data; treat that as a referral, not as low risk.
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

    # Distinguish "no data published" from "no crime here". A force that has
    # withdrawn from the feed can still leak a handful of records, so check
    # publication whenever the area looks quiet rather than only at exactly
    # zero. Results are cached per force.
    force, publishes = None, True
    if monthly_avg < CRIME_QUIET_THRESHOLD:
        async with httpx.AsyncClient(timeout=20.0) as client:
            force = await _locate_force(client, lat, lng)
            if force:
                publishes = await _force_publishes(client, force)

    if not publishes:
        return {
            "postcode": postcode_clean,
            "latitude": lat,
            "longitude": lng,
            "crime_index": None,
            "crime_band": "DATA_UNAVAILABLE",
            "data_available": False,
            "police_force": force,
            "all_crimes_total": total_all_crimes,
            "months_analysed": months_fetched,
            "note": (
                f"The police force covering this postcode ({force}) does not publish "
                "street-level crime data. Crime exposure could not be "
                "assessed — refer for manual review rather than assuming "
                "low risk."
            ),
            "data_source": "data.police.uk street-level crime API",
            "errors": errors if errors else None,
        }

    vs_national = round(monthly_avg / NATIONAL_AVG_MONTHLY_PROPERTY_CRIMES, 1)

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
        "data_available": True,
        "vs_national_average": vs_national,
        "national_avg_monthly_property_crimes": NATIONAL_AVG_MONTHLY_PROPERTY_CRIMES,
        "crime_summary": (
            f"{round(monthly_avg)} property crimes per month within ~1 mile — "
            f"about {vs_national}x the national average"
        ),
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
# Tool 5: get_property_sale_history (HM Land Registry Price Paid)
# ---------------------------------------------------------------------------

LAND_REGISTRY_URL = (
    "https://landregistry.data.gov.uk/data/ppi/transaction-record.json"
)

# Buildings cover should reflect REBUILD cost, not market value. Rebuild
# excludes land, so it is normally well below the sale price in most of the
# country — a sum insured under the sale price is ordinary, not a red flag.
# These bounds only catch the implausible ends.
SUM_INSURED_OVER_RATIO = 2.5    # cover far above value — possible overinsurance
SUM_INSURED_UNDER_RATIO = 0.25  # cover far below value — possible underinsurance


def _lr_date(value) -> tuple[str, int | None]:
    """
    Normalise a Land Registry transactionDate to (ISO date, year).

    The API returns RFC-1123-ish strings such as "Thu, 14 Aug 2014", not
    ISO dates, so sorting or slicing the raw value gives wrong answers.
    """
    if not isinstance(value, str) or not value.strip():
        return "", None
    for fmt in ("%a, %d %b %Y", "%Y-%m-%d", "%d %b %Y"):
        try:
            parsed = datetime.strptime(value.strip(), fmt)
            return parsed.strftime("%Y-%m-%d"), parsed.year
        except ValueError:
            continue
    return value, None


def _lr_label(node) -> str | None:
    """Pull the human label out of a linked-data node."""
    if not isinstance(node, dict):
        return node if isinstance(node, str) else None
    for key in ("prefLabel", "label"):
        values = node.get(key)
        if isinstance(values, list) and values:
            first = values[0]
            if isinstance(first, dict):
                return first.get("_value")
            if isinstance(first, str):
                return first
    return None


@mcp.tool()
async def get_property_sale_history(
    postcode: str,
    house_number: str = "",
    sum_insured: float = 0,
) -> dict:
    """
    Returns HM Land Registry Price Paid sale history for a UK postcode.

    Use to sanity-check the sum insured at submission stage. Pass
    house_number to narrow to one property; omit it to see the postcode.

    IMPORTANT when interpreting: buildings sum insured should be the
    REBUILD cost, which excludes land and is normally LOWER than the sale
    price. A sum insured below the last sale price is therefore normal and
    not by itself a concern. Only marked ratios are worth acting on.

    Sale prices are historic and not inflation-adjusted — check sale_year
    before drawing conclusions from an old transaction.

    Args:
        postcode: UK postcode, e.g. "BS9 3AA"
        house_number: Optional building number/name to match exactly
        sum_insured: Optional requested cover, to compute the ratio check

    Returns sales list plus, when sum_insured is given, a valuation_check.
    """
    postcode_clean = postcode.strip().upper()

    params = {
        "propertyAddress.postcode": postcode_clean,
        "_pageSize": "20",
        "_sort": "-transactionDate",
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                LAND_REGISTRY_URL,
                params=params,
                headers={"Accept": "application/json"},
            )
            if resp.status_code != 200:
                return {
                    "error": f"Land Registry returned HTTP {resp.status_code}",
                    "postcode": postcode_clean,
                    "data_source": "HM Land Registry Price Paid Data",
                }
            items = resp.json().get("result", {}).get("items", [])
    except httpx.HTTPError as e:
        return {
            "error": f"Land Registry unreachable: {e}",
            "postcode": postcode_clean,
            "data_source": "HM Land Registry Price Paid Data",
        }

    sales = []
    for item in items:
        address = item.get("propertyAddress", {}) or {}
        paon = str(address.get("paon", "") or "")

        if house_number and paon.strip().upper() != house_number.strip().upper():
            continue

        iso_date, year = _lr_date(item.get("transactionDate"))
        sales.append({
            "price_paid": item.get("pricePaid"),
            "transaction_date": iso_date,
            "sale_year": year,
            "property_type": _lr_label(item.get("propertyType")),
            "estate_type": _lr_label(item.get("estateType")),
            "new_build": item.get("newBuild"),
            "address": " ".join(
                str(address.get(k, "")) for k in ("paon", "street", "town")
                if address.get(k)
            ).strip(),
        })

    if not sales:
        return {
            "postcode": postcode_clean,
            "house_number": house_number or None,
            "sales_found": 0,
            "note": (
                "No Price Paid records for this postcode. Common for new "
                "builds, properties unsold since 1995, and non-residential "
                "addresses. Absence of a record is not itself a risk signal."
            ),
            "data_source": "HM Land Registry Price Paid Data",
        }

    sales.sort(key=lambda x: x["transaction_date"] or "", reverse=True)
    latest = sales[0]
    prices = sorted(s["price_paid"] for s in sales if s.get("price_paid"))
    median_price = (
        (prices[len(prices) // 2 - 1] + prices[len(prices) // 2]) // 2
        if len(prices) % 2 == 0 else prices[len(prices) // 2]
    ) if prices else None

    result = {
        "postcode": postcode_clean,
        "house_number": house_number or None,
        "sales_found": len(sales),
        "latest_sale": latest,
        "postcode_median_price": median_price,
        "sales": sales[:10],
        "data_source": "HM Land Registry Price Paid Data",
    }

    # A postcode holds many different properties. Comparing the cover on one
    # house against whatever sold most recently nearby produces nonsense —
    # a neighbouring mansion makes an ordinary policy look underinsured. So
    # only judge when we can identify the subject property.
    if sum_insured and not house_number:
        result["valuation_check"] = {
            "sum_insured": sum_insured,
            "verdict": "NOT_ASSESSED",
            "note": (
                "No house number supplied, so the sum insured could not be "
                "compared against this property's own sale history. The "
                "postcode contains multiple properties at different values "
                f"(£{prices[0]:,} to £{prices[-1]:,} across {len(prices)} "
                "sales). Supply house_number to run the check."
            ),
        }
    elif sum_insured and latest.get("price_paid"):
        ratio = round(sum_insured / latest["price_paid"], 2)
        if ratio >= SUM_INSURED_OVER_RATIO:
            verdict = "POSSIBLE_OVERINSURANCE"
            note = (
                f"Sum insured is {ratio}x the last sale price "
                f"(£{latest['price_paid']:,} in {latest['sale_year']}). "
                "Verify the rebuild-cost assessment."
            )
        elif ratio <= SUM_INSURED_UNDER_RATIO:
            verdict = "POSSIBLE_UNDERINSURANCE"
            note = (
                f"Sum insured is only {ratio}x the last sale price "
                f"(£{latest['price_paid']:,} in {latest['sale_year']}). "
                "Risk of average being applied at claim stage."
            )
        else:
            verdict = "PLAUSIBLE"
            note = (
                f"Sum insured is {ratio}x the last sale price, within the "
                "normal range for rebuild cost versus market value."
            )

        result["valuation_check"] = {
            "sum_insured": sum_insured,
            "latest_sale_price": latest["price_paid"],
            "sale_year": latest["sale_year"],
            "ratio_to_sale_price": ratio,
            "verdict": verdict,
            "note": note,
            "caveat": (
                "Sale price is historic and not inflation-adjusted; rebuild "
                "cost excludes land value."
            ),
        }

    return result


# ---------------------------------------------------------------------------
# Tool 6: check_business_registrations (Companies House)
# ---------------------------------------------------------------------------

COMPANIES_HOUSE_URL = (
    "https://api.company-information.service.gov.uk/advanced-search/companies"
)


@mcp.tool()
async def check_business_registrations(postcode: str, house_number: str = "") -> dict:
    """
    Returns companies registered at a UK postcode (Companies House).

    Use to detect a residential property also serving as a registered
    business address, which affects occupancy risk and may fall outside a
    standard residential policy.

    Interpret with care: a registered office is an administrative address,
    not proof of trading activity at the property. Many sole traders
    register at a home address and carry on no business there. Treat a hit
    as something to ask the broker about, not as an automatic decline.

    Requires COMPANIES_HOUSE_API_KEY. Without it the tool reports that the
    check could not be run, rather than implying no businesses exist.

    Args:
        postcode: UK postcode, e.g. "BS9 3AA"
        house_number: Optional building number/name to narrow the match
    """
    import os

    postcode_clean = postcode.strip().upper()
    api_key = os.environ.get("COMPANIES_HOUSE_API_KEY", "").strip()

    if not api_key:
        return {
            "postcode": postcode_clean,
            "check_performed": False,
            "note": (
                "COMPANIES_HOUSE_API_KEY is not configured, so business "
                "registrations could not be checked. Do not treat this as "
                "confirmation that the property has no business use."
            ),
            "data_source": "Companies House API",
        }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                COMPANIES_HOUSE_URL,
                params={"location": postcode_clean, "size": "50"},
                auth=(api_key, ""),
            )
            if resp.status_code == 401:
                return {
                    "postcode": postcode_clean,
                    "check_performed": False,
                    "error": "Companies House rejected the API key (HTTP 401).",
                    "data_source": "Companies House API",
                }
            if resp.status_code != 200:
                return {
                    "postcode": postcode_clean,
                    "check_performed": False,
                    "error": f"Companies House returned HTTP {resp.status_code}",
                    "data_source": "Companies House API",
                }
            items = resp.json().get("items", [])
    except httpx.HTTPError as e:
        return {
            "postcode": postcode_clean,
            "check_performed": False,
            "error": f"Companies House unreachable: {e}",
            "data_source": "Companies House API",
        }

    companies = []
    for item in items:
        office = item.get("registered_office_address", {}) or {}
        premises = str(office.get("premises", "") or "")
        line1 = str(office.get("address_line_1", "") or "")

        if house_number:
            wanted = house_number.strip().upper()
            if wanted not in premises.upper() and not line1.upper().startswith(wanted):
                continue

        companies.append({
            "company_name": item.get("company_name"),
            "company_number": item.get("company_number"),
            "company_status": item.get("company_status"),
            "company_type": item.get("company_type"),
            "incorporated_on": item.get("date_of_creation"),
            "sic_codes": item.get("sic_codes"),
            "registered_office": ", ".join(
                str(office.get(k, "")) for k in
                ("premises", "address_line_1", "locality", "postal_code")
                if office.get(k)
            ),
        })

    active = [c for c in companies if c.get("company_status") == "active"]

    return {
        "postcode": postcode_clean,
        "house_number": house_number or None,
        "check_performed": True,
        "companies_found": len(companies),
        "active_companies": len(active),
        "business_address_flag": bool(active),
        "companies": companies[:10],
        "note": (
            f"{len(active)} active compan{'y' if len(active) == 1 else 'ies'} "
            "registered at this address. A registered office is not proof of "
            "trading at the property — confirm actual use with the broker."
            if active else
            "No active companies registered at this address."
        ),
        "data_source": "Companies House API",
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", "8001"))
    print(f"Starting UW Risk Tools MCP Server v4 on http://0.0.0.0:{port}")
    mcp.run(transport="streamable-http", host="0.0.0.0", port=port)

