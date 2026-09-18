"""
Risk Tools MCP Server v4
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
    python mcp_servers/risk_server_v4.py
"""

import json
import httpx
from datetime import datetime, timedelta
from fastmcp import FastMCP

mcp = FastMCP("uw-risk-tools-v4")


# ---------------------------------------------------------------------------
# Shared helper: postcode → lat/lng via postcodes.io
# ---------------------------------------------------------------------------

def _month_offset(months_back: int) -> str:
    """
    The YYYY-MM string this many whole calendar months before now.

    timedelta(days=30 * n) drifts: near a month end it can request the same
    month twice or skip one entirely.
    """
    now = datetime.now()
    index = now.year * 12 + (now.month - 1) - months_back
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


async def _geocode(postcode: str) -> tuple[float, float]:
    """Convert a UK postcode to lat/lng. Raises ValueError if not found."""
    lat, lng, _, _ = await _geocode_full(postcode)
    return lat, lng


async def _geocode_full(postcode: str) -> tuple[float, float, float | None, float | None]:
    """
    Convert a UK postcode to (lat, lng, easting, northing).

    postcodes.io returns British National Grid coordinates alongside WGS84,
    which is what the RoFRS shapefile uses — so no reprojection is needed.
    """
    clean = postcode.strip().upper().replace(" ", "")
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"https://api.postcodes.io/postcodes/{clean}")

    if resp.status_code == 404:
        raise ValueError(f"Postcode '{postcode}' not found.")
    resp.raise_for_status()

    result = resp.json().get("result")
    if not result:
        raise ValueError(f"No geocode result for postcode '{postcode}'.")

    easting = result.get("eastings")
    northing = result.get("northings")
    return (
        float(result["latitude"]),
        float(result["longitude"]),
        float(easting) if easting is not None else None,
        float(northing) if northing is not None else None,
    )


# ---------------------------------------------------------------------------
# Flood risk baseline: EA Risk of Flooding from Rivers and Sea (RoFRS)
#
# Replaces the previous hardcoded postcode -> flood zone table. That table
# keyed on outward code, which cannot express flood risk: TW1 3DY (Eel Pie
# Island, in the Thames) is High while TW1 3NP 150m away is Very Low, yet
# both shared one entry. Cross-checking it against RoFRS also found five
# London districts marked Zone 3a whose every sampled postcode falls outside
# any flood polygon.
#
# RoFRS bands are the EA's own classification and account for flood
# defences, unlike Flood Map for Planning zones:
#   High      >1 in 30 annual chance
#   Medium    1 in 100 to 1 in 30
#   Low       1 in 1000 to 1 in 100
#   Very Low  <1 in 1000
#
# Coverage is Greater London. Outside it the tool reports the risk as
# unassessed rather than guessing low.
# ---------------------------------------------------------------------------

from rofrs import band_at, in_coverage, BAND_SEVERITY

# Live EA warning severity -> the RoFRS band it implies. A warning only ever
# raises the assessed risk; it never lowers the mapped baseline.
EA_SEVERITY_TO_BAND = {1: "High", 2: "High", 3: "Medium"}

# Flood Re excludes homes built on or after 1 January 2009, so that new
# development on floodplains is not subsidised by the levy.
FLOOD_RE_BUILD_CUTOFF = 2009


# ---------------------------------------------------------------------------
# Tool 1: get_flood_zone
# Source: Environment Agency flood-monitoring API + static fallback layer
# Docs:   https://environment.data.gov.uk/flood-monitoring/doc/reference
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_flood_zone(postcode: str, year_built: int = 0) -> dict:
    """
    Returns flood risk data for a UK property postcode.

    Uses a two-layer approach:
      Layer 1 (mapped): EA Risk of Flooding from Rivers and Sea (RoFRS)
        polygons, looked up at the property's own grid reference. Gives
        realistic results year-round regardless of current weather.
      Layer 2 (live): Environment Agency real-time flood warnings API.
        Raises the assessed risk if an active warning exists nearby.

    Risk bands (EA RoFRS classification):
      High      >1 in 30 annual chance    — decline territory, see guidelines
      Medium    1 in 100 to 1 in 30       — refer
      Low       1 in 1000 to 1 in 100     — acceptable, loading applies
      Very Low  <1 in 1000                — standard terms

    RoFRS accounts for flood defences, unlike Flood Map for Planning zones.

    flood_risk_band is "Unassessed" where the property lies outside the
    mapped dataset. Treat that as a referral, never as low risk.

    Pass year_built to get a real Flood Re answer. Cession requires the
    property to predate 1 January 2009, so without a build year the tool
    reports flood_re_eligible false and explains why in flood_re_note —
    it will not guess.

    Coverage: the bundled RoFRS extract is Greater London. Scotland uses
    SEPA, Wales NRW, Northern Ireland DfI Rivers.
    """
    postcode_clean = postcode.strip().upper()
    try:
        lat, lng, easting, northing = await _geocode_full(postcode_clean)
    except ValueError as e:
        return {"error": str(e), "postcode": postcode_clean}

    # Layer 1: mapped RoFRS band at this property's grid reference
    mapped_band = None
    mapped = False
    if easting is not None and northing is not None and in_coverage(easting, northing):
        mapped = True
        # None inside coverage means no flood polygon here, i.e. negligible.
        mapped_band = band_at(easting, northing) or "Very Low"


    # Layer 2: EA live warnings
    ea_band = None
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

            ea_band = EA_SEVERITY_TO_BAND.get(severity_level)

    except httpx.RequestError:
        pass  # EA API unavailable — fall through to static layer

    # Resolve the final band. A live warning can only raise the assessed
    # risk, never lower the mapped baseline. Outside the mapped extent the
    # answer is "Unassessed": a property we have no data for must refer, not
    # pass as low risk.
    candidates = [b for b in (ea_band, mapped_band) if b]
    if candidates:
        flood_risk_band = max(candidates, key=lambda b: BAND_SEVERITY.get(b, 0))
    elif mapped:
        flood_risk_band = "Very Low"
    else:
        flood_risk_band = "Unassessed"

    # Flood Re exists for homes at genuine flood risk, so only the two
    # higher bands qualify. Eligibility also requires a build date before
    # 1 January 2009 — a scheme rule, not a risk judgement. Reporting
    # eligibility from the band alone made every new-build on a floodplain
    # look cessionable, which reversed the decision on exactly the cases
    # the cut-off exists to catch.
    band_qualifies = flood_risk_band in ("High", "Medium")
    if not year_built:
        # Build date unknown: report what we can check and say what we cannot.
        flood_re_eligible = False
        flood_re_note = (
            "Flood Re eligibility could not be confirmed: no build year was "
            "supplied. The flood band "
            + ("qualifies" if band_qualifies else "does not qualify")
            + ". Cession also requires construction before 1 January 2009, "
            "plus council tax band A-G and primary residence use, which this "
            "tool does not check."
        )
    elif year_built >= FLOOD_RE_BUILD_CUTOFF:
        flood_re_eligible = False
        flood_re_note = (
            f"Not eligible for Flood Re: built {year_built}, on or after the "
            f"{FLOOD_RE_BUILD_CUTOFF} cut-off. Properties built from "
            f"{FLOOD_RE_BUILD_CUTOFF} are excluded from the scheme regardless "
            "of flood risk."
        )
    else:
        flood_re_eligible = band_qualifies
        flood_re_note = (
            f"Built {year_built}, before the {FLOOD_RE_BUILD_CUTOFF} cut-off, "
            "and the flood band qualifies. Final cession still depends on "
            "council tax band A-G and primary residence use, which this tool "
            "does not check."
            if band_qualifies else
            f"Built {year_built}, before the {FLOOD_RE_BUILD_CUTOFF} cut-off, "
            "but the flood band does not qualify for cession."
        )

    if not mapped:
        source = "Outside the RoFRS mapped extent (London only) — risk unassessed"
    elif ea_band and BAND_SEVERITY.get(ea_band, 0) > BAND_SEVERITY.get(mapped_band or "", 0):
        source = "Environment Agency flood-monitoring API (live warning)"
    else:
        source = "EA Risk of Flooding from Rivers and Sea (RoFRS), 2018 + EA monitoring API"

    result = {
        "postcode": postcode_clean,
        "latitude": lat,
        "longitude": lng,
        "easting": easting,
        "northing": northing,
        "flood_risk_band": flood_risk_band,
        "risk_assessed": mapped,
        "mapped_band": mapped_band,
        "ea_live_warning_band": ea_band,
        "ea_severity_level": severity_level,
        "active_warnings_within_5km": active_warnings,
        "warning_descriptions": warning_descriptions[:3],
        "flood_re_eligible": flood_re_eligible,
        "flood_re_band_qualifies": band_qualifies,
        "flood_re_note": flood_re_note,
        "band_definition": {
            "High": ">1 in 30 annual chance",
            "Medium": "1 in 100 to 1 in 30",
            "Low": "1 in 1000 to 1 in 100",
            "Very Low": "<1 in 1000",
        }.get(flood_risk_band),
        "data_source": source,
        "coverage": (
            "RoFRS extract covers Greater London. Scotland: SEPA. "
            "Wales: NRW. NI: DfI Rivers."
        ),
    }

    if not mapped:
        result["note"] = (
            "This postcode is outside the bundled RoFRS extract, so flood "
            "risk could not be assessed. Refer for manual review — do not "
            "treat an unassessed property as low risk."
        )

    return result


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
    "bicycle-theft",
    "violent-crime",
}

# Calibrated multiplier — see calibration notes above
CRIME_INDEX_MULTIPLIER = 1.0

# Reference baseline for property crimes per month within the ~1 mile radius
# the police API returns.
#
# Measured, not assumed: 100 postcodes drawn at random from postcodes.io and
# kept where region == "London", queried over 2026-05..2026-07 with the same
# radius and category filter this tool uses. All 100 returned usable data.
#
#   min 13.7 | p10 56 | p25 112 | MEDIAN 277 | p75 620 | p90 1459 | max 3182
#
# The distribution is heavily right-skewed (mean 560 vs median 277), so the
# median is the centre, not the mean.
#
# Scope is London. An earlier attempt at a national figure was abandoned:
# most UK postcodes are rural, which dragged the median so low that every
# urban property read as a huge multiple of it. Comparing a non-London
# property against this baseline overstates how unusual it is.
LONDON_MEDIAN_MONTHLY_PROPERTY_CRIMES = 277

# Band boundaries in property crimes per month, taken from percentiles of
# the same sample. Each value is the lower bound of the next band up.
#   LOW        below p25   — quieter than about 75% of London
#   MEDIUM     p25 to p75  — the typical London range, median 277 sits here
#   HIGH       p75 to p90  — busier than about 75% of London
#   VERY_HIGH  above p90   — the top tenth
CRIME_BAND_THRESHOLDS = {
    "LOW": 112,       # p25
    "MEDIUM": 620,    # p75
    "HIGH": 1459,     # p90
}

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
        try:
            resp = await client.get(
                "https://data.police.uk/api/crimes-no-location",
                params={
                    "category": "all-crime",
                    "force": force,
                    "date": _month_offset(months_back),
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

    Bands are percentiles of a measured London sample (100 random postcodes,
    2026-05..07), in property crimes per month within ~1 mile:
      LOW       (<112)      — quieter than ~75% of London, standard rate
      MEDIUM    (112–619)   — typical London range, check security
      HIGH      (620–1458)  — busier than ~75% of London, 10% loading
      VERY_HIGH (>=1459)    — top ~10% of London, refer to senior underwriter

    crime_index is retained for continuity but saturates at 100 for any
    urban postcode — use vs_london_median and crime_summary instead.

    Only counts property-relevant categories: burglary, vehicle crime,
    theft, robbery, shoplifting, criminal damage/arson.

    Calibration: monthly average × 1.0, capped at 100. The index saturates
    for any urban area, so prefer vs_london_median and crime_summary when
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
    empty_months = 0
    errors = []

    async with httpx.AsyncClient(timeout=20.0) as client:
        for months_back in range(2, 5):
            month_str = _month_offset(months_back)

            try:
                resp = await client.get(
                    "https://data.police.uk/api/crimes-street/all-crime",
                    params={"lat": lat, "lng": lng, "date": month_str}
                )
                if resp.status_code == 200:
                    crimes = resp.json()
                    if not crimes:
                        # HTTP 200 with an empty list means either a genuinely
                        # quiet area or a month this force has not published.
                        # Record it separately: it counts as a real zero once
                        # we confirm the force publishes, and excludes the
                        # postcode if it does not.
                        errors.append(f"{month_str}: no records returned")
                        empty_months += 1
                        continue
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

    # An area where every month came back empty is either crime-free or
    # covered by a force that publishes nothing. Ask before deciding: a
    # false LOW would let a high-crime city centre through at standard rates.
    if months_fetched == 0 and empty_months:
        async with httpx.AsyncClient(timeout=20.0) as client:
            force = await _locate_force(client, lat, lng)
            publishes = await _force_publishes(client, force) if force else True
        if publishes:
            months_fetched = empty_months        # genuine zeros
        else:
            return {
                "postcode": postcode_clean,
                "latitude": lat,
                "longitude": lng,
                "crime_index": None,
                "crime_band": "DATA_UNAVAILABLE",
                "data_available": False,
                "police_force": force,
                "all_crimes_total": 0,
                "months_analysed": 0,
                "note": (
                    f"The police force covering this postcode ({force}) does "
                    "not publish street-level crime data. Crime exposure could "
                    "not be assessed — refer for manual review rather than "
                    "assuming low risk."
                ),
                "data_source": "data.police.uk street-level crime API",
                "errors": errors or None,
            }

    if months_fetched == 0:
        return {
            "error": "Could not retrieve crime data — Police API unavailable.",
            "postcode": postcode_clean,
            "errors": errors,
            "data_source": "data.police.uk"
        }

    monthly_avg = total_property_crimes / months_fetched
    print(f"get_crime_index: {total_property_crimes} property crimes over {months_fetched} months → {monthly_avg:.1f}/month average for {postcode_clean}")
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

    vs_median = round(monthly_avg / LONDON_MEDIAN_MONTHLY_PROPERTY_CRIMES, 1)

    # Band on the measured distribution, not on the saturating index. The
    # index caps at 100, so every urban postcode hit VERY_HIGH: the median
    # London postcode (277/month) scored 100 and was treated as an extreme
    # risk. Thresholds are percentiles of the 100-postcode London sample, so
    # a typical London property now lands in the middle of the scale.
    if monthly_avg < CRIME_BAND_THRESHOLDS["LOW"]:
        band = "LOW"                 # quieter than ~75% of London
    elif monthly_avg < CRIME_BAND_THRESHOLDS["MEDIUM"]:
        band = "MEDIUM"              # the typical London range
    elif monthly_avg < CRIME_BAND_THRESHOLDS["HIGH"]:
        band = "HIGH"                # busier than ~75% of London
    else:
        band = "VERY_HIGH"           # top ~10% of London

    return {
        "postcode": postcode_clean,
        "latitude": lat,
        "longitude": lng,
        "crime_index": index,
        "crime_band": band,
        "data_available": True,
        "vs_london_median": vs_median,
        "band_thresholds_monthly_crimes": CRIME_BAND_THRESHOLDS,
        "london_median_monthly_property_crimes": LONDON_MEDIAN_MONTHLY_PROPERTY_CRIMES,
        "crime_summary": (
            f"{round(monthly_avg)} property crimes per month within ~1 mile — "
            f"about {vs_median}x the median London postcode"
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
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", "8001"))
    print(f"Starting UW Risk Tools MCP Server v4 on http://0.0.0.0:{port}")
    mcp.run(transport="streamable-http", host="0.0.0.0", port=port)

