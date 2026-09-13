"""
Underwriting Risk Assessment - Streamlit UI
-------------------------------------------
Provides a broker submission workflow and a read-only Azure Queue Storage
monitor for ACCEPT, DECLINE, and REFER decisions.

    Terminal 1:  python mcp_servers/risk_server.py
    Terminal 2:  uvicorn api.main:app --port 8010
    Terminal 3:  streamlit run app.py
"""

import json
import logging
import os
from datetime import datetime

import requests
import streamlit as st
from azure.identity import DefaultAzureCredential
from azure.storage.queue import QueueClient
from dotenv import load_dotenv

load_dotenv()

API_URL = os.environ.get("UW_API_URL", "http://127.0.0.1:8010")
API_TIMEOUT = float(os.environ.get("UW_API_TIMEOUT", "300"))
STORAGE_ACCOUNT = os.environ.get("AZURE_STORAGE_ACCOUNT_NAME", "").strip()
STORAGE_CONNECTION_STRING = os.environ.get(
    "AZURE_STORAGE_CONNECTION_STRING", ""
).strip()
QUEUE_PEEK_LIMIT = 32
DECISION_QUEUES = {
    "ACCEPT": os.environ.get("UW_ACCEPT_QUEUE", "uw-decisions-accept"),
    "DECLINE": os.environ.get("UW_DECLINE_QUEUE", "uw-decisions-decline"),
    "REFER": os.environ.get("UW_REFER_QUEUE", "uw-decisions-refer"),
}
logger = logging.getLogger(__name__)


st.set_page_config(
    page_title="Underwriting Risk Assessment",
    page_icon=":material/shield:",
    layout="wide",
)


def request_assessment(payload: dict) -> dict:
    """POST the submission to the API service and return the decision."""
    response = requests.post(
        f"{API_URL}/assess", json=payload, timeout=API_TIMEOUT
    )
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        raise RuntimeError(f"API returned {response.status_code}: {detail}")
    return response.json()


@st.cache_data(ttl=30)
def request_findings(payload: dict) -> dict:
    """POST the submission to the tools-only endpoint and return raw findings."""
    response = requests.post(
        f"{API_URL}/findings", json=payload, timeout=API_TIMEOUT
    )
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        raise RuntimeError(f"API returned {response.status_code}: {detail}")
    return response.json()


def fetch_health() -> dict:
    """Ask the API for its status, provider, and model."""
    response = requests.get(f"{API_URL}/health", timeout=10)
    response.raise_for_status()
    return response.json()


@st.cache_resource
def default_storage_credential() -> DefaultAzureCredential:
    """Reuse one Azure credential chain across Streamlit reruns."""
    return DefaultAzureCredential()


def build_queue_client(queue_name: str) -> QueueClient:
    """Build a queue client from the configured connection string or identity."""
    if STORAGE_CONNECTION_STRING:
        return QueueClient.from_connection_string(
            STORAGE_CONNECTION_STRING,
            queue_name=queue_name,
        )

    if not STORAGE_ACCOUNT:
        raise RuntimeError(
            "Set AZURE_STORAGE_ACCOUNT_NAME or AZURE_STORAGE_CONNECTION_STRING."
        )

    return QueueClient(
        account_url=f"https://{STORAGE_ACCOUNT}.queue.core.windows.net",
        queue_name=queue_name,
        credential=default_storage_credential(),
    )


def _to_iso(value) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _parse_message(content: str) -> tuple[dict, bool]:
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return {"raw_message": content}, False
    if not isinstance(payload, dict):
        return {"raw_message": content}, False
    return payload, True


def _display_value(value, fallback: str = "Not provided") -> str:
    if value is None or value == "":
        return fallback
    return str(value)


def _humanize(value) -> str:
    if value is None or value == "":
        return "Not provided"
    return str(value).replace("_", " ").replace("-", " ").title()


def _format_datetime(value) -> str:
    if not value:
        return "Not provided"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    return parsed.astimezone().strftime("%d %b %Y, %H:%M")


def _format_date(value) -> str:
    if not value:
        return "Not provided"
    try:
        return datetime.fromisoformat(str(value)).strftime("%d %b %Y")
    except ValueError:
        return str(value)


def _format_currency(value) -> str:
    if value is None or value == "":
        return "Not provided"
    try:
        return f"£{float(value):,.0f}"
    except (TypeError, ValueError):
        return str(value)


def _yes_no(value) -> str:
    if isinstance(value, str):
        value = value.strip().lower() in {"1", "true", "yes", "on"}
    return "Yes" if value else "No"


def _render_field(label: str, value) -> None:
    st.caption(label)
    st.write(_display_value(value))


@st.cache_data(ttl=15, show_spinner=False)
def fetch_queue_snapshot(queue_name: str) -> dict:
    """Peek at visible messages without changing their queue state."""
    queue_client = build_queue_client(queue_name)
    properties = queue_client.get_queue_properties()
    messages = []

    for message in queue_client.peek_messages(max_messages=QUEUE_PEEK_LIMIT):
        payload, valid_json = _parse_message(message.content)
        messages.append(
            {
                "id": message.id,
                "inserted_at": _to_iso(getattr(message, "insertion_time", None)),
                "expires_at": _to_iso(getattr(message, "expiration_time", None)),
                "dequeue_count": getattr(message, "dequeue_count", 0),
                "payload": payload,
                "valid_json": valid_json,
            }
        )

    return {
        "approximate_count": properties.approximate_message_count or 0,
        "messages": messages,
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def render_service_status() -> None:
    with st.sidebar:
        st.subheader("Service")
        st.caption(f"API: `{API_URL}`")
        try:
            health = fetch_health()
        except Exception as exc:
            st.error("API unreachable")
            st.caption(f"{type(exc).__name__}: {exc}")
            st.code("uvicorn api.main:app --port 8010", language="bash")
        else:
            st.success(f"Connected · `{health['provider']}`")
            st.caption(f"Model: `{health['model']}`")
            if health.get("detail"):
                st.warning(health["detail"])


def render_findings(findings: dict) -> None:
    """
    Show the calibrated tool data with no interpretation.

    This is deliberately flat: numbers and bands as the data sources report
    them. Deciding what any of it means for the risk is the agent's job,
    shown in the section below.
    """
    st.subheader("Findings")
    st.caption(
        "Calibrated data from the risk tools — no AI. "
        "Facts only; no judgement about what they mean for this risk."
    )

    flood = findings.get("flood") or {}
    crime = findings.get("crime") or {}
    claims = findings.get("claims") or {}
    validation = findings.get("validation") or {}

    col_flood, col_crime, col_claims = st.columns(3)

    with col_flood:
        st.markdown("**Flood**")
        if flood.get("error"):
            st.warning(flood["error"])
        else:
            st.metric("Flood zone", _display_value(flood.get("flood_zone")))
            st.caption(f"Flood Re eligible: {_yes_no(flood.get('flood_re_eligible'))}")
            if flood.get("ea_severity_level"):
                st.caption(f"EA warning: {flood['ea_severity_level']}")
            if flood.get("data_source"):
                st.caption(f"Source: {flood['data_source']}")

    with col_crime:
        st.markdown("**Property crime**")
        if crime.get("error"):
            st.warning(crime["error"])
        elif crime.get("data_available") is False:
            st.metric("Crime exposure", "No data")
            st.caption(crime.get("note", "The covering police force publishes no data."))
        else:
            ratio = crime.get("vs_national_average")
            st.metric(
                "Vs national average",
                f"{ratio}x" if ratio is not None else "Unknown",
            )
            if crime.get("crime_summary"):
                st.caption(crime["crime_summary"])
            st.caption(f"Band: {_display_value(crime.get('crime_band'))}")

    with col_claims:
        st.markdown("**Claims history**")
        if claims.get("error"):
            st.warning(claims["error"])
        else:
            st.metric(
                "Verified claims (5yr)",
                _display_value(claims.get("verified_claims_count")),
            )
            types = claims.get("claim_types") or []
            st.caption(f"Types: {', '.join(types) if types else 'None'}")
            st.caption(
                f"Anomaly vs declared: {_yes_no(claims.get('claims_anomaly_detected'))}"
            )

    flags = validation.get("flags") or []
    if flags or validation.get("summary"):
        st.markdown("**Submission validation**")
        if validation.get("summary"):
            st.caption(validation["summary"])
        for flag in flags:
            st.caption(f"- {flag}")

    with st.expander("Raw tool output"):
        st.json(findings)


def render_assessment_result(result: dict) -> None:
    st.subheader("AI interpretation")
    st.caption(
        "What the agent concluded from the findings above, weighed against "
        "the underwriting guidelines."
    )

    if result["decision"] == "ACCEPT":
        st.success(f"ACCEPT · Confidence: {result['confidence']}")
    elif result["decision"] == "REFER":
        st.warning(f"REFER · Confidence: {result['confidence']}")
    else:
        st.error(f"DECLINE · Confidence: {result['confidence']}")

    col_a, col_b = st.columns(2)

    with col_a:
        st.subheader("Rationale")
        st.write(result["rationale"])

        if result.get("refer_reason"):
            st.info(f"Refer reason: {result['refer_reason']}")

        if result.get("recommended_premium_loading"):
            st.metric(
                "Premium loading",
                f"+{result['recommended_premium_loading']:.1f}%",
            )

        st.metric(
            "Flood Re eligible",
            "Yes" if result["flood_re_eligible"] else "No",
        )
        st.metric("Processing time", f"{result['processing_time_ms']}ms")

    with col_b:
        st.subheader("Risk Flags")
        if result["risk_flags"]:
            for flag in result["risk_flags"]:
                st.code(flag, language=None)
        else:
            st.write("No risk flags raised.")

    with st.expander("Raw agent output"):
        st.code(result["raw_agent_output"], language="json")


def render_assessment_page() -> None:
    st.title("Underwriting Risk Assessment")
    st.caption("Agentic underwriting over MCP tools and a RAG guidelines index")

    with st.form("submission_form"):
        st.subheader("Applicant")
        col1, col2, col3 = st.columns(3)
        applicant_name = col1.text_input("Full name", value="Jane Smith")
        date_of_birth = col2.text_input(
            "Date of birth (YYYY-MM-DD)", value="1978-06-15"
        )
        occupation = col3.text_input("Occupation", value="Teacher")

        st.subheader("Risk Location")
        col4, col5 = st.columns(2)
        property_address = col4.text_input(
            "Address", value="12 Riverside Close, Bristol"
        )
        property_postcode = col5.text_input("Postcode", value="BS1 4DJ")

        col6, col7, col8, col9 = st.columns(4)
        property_type = col6.selectbox(
            "Property type", ["detached", "semi", "flat", "commercial"]
        )
        year_built = col7.number_input(
            "Year built", min_value=1600, max_value=2026, value=1912
        )
        construction = col8.selectbox(
            "Construction", ["brick", "timber", "concrete"]
        )
        num_storeys = col9.number_input(
            "Storeys", min_value=1, max_value=20, value=2
        )

        st.subheader("Coverage")
        col10, col11, col12 = st.columns(3)
        product_type = col10.selectbox(
            "Product", ["buildings", "contents", "combined"]
        )
        sum_insured = col11.number_input(
            "Sum insured (£)",
            min_value=10000,
            max_value=10000000,
            value=425000,
            step=5000,
        )
        policy_start_date = col12.text_input(
            "Start date (YYYY-MM-DD)", value="2026-05-01"
        )

        st.subheader("Claims History")
        col13, col14, col15 = st.columns(3)
        claims_last_5_years = col13.number_input(
            "Claims in last 5 years", min_value=0, max_value=20, value=2
        )
        prior_claim_types_str = col14.text_input(
            "Claim types (comma-separated)",
            value="escape_of_water, subsidence",
        )
        outstanding_claims = col15.checkbox("Outstanding claims?", value=False)

        broker_reference = st.text_input(
            "Broker reference (optional)", value="BRK-2026-00142"
        )

        submitted = st.form_submit_button(
            "Run Assessment",
            type="primary",
            icon=":material/play_arrow:",
            use_container_width=True,
        )

    if not submitted:
        return

    payload = {
        "applicant_name": applicant_name,
        "date_of_birth": date_of_birth,
        "occupation": occupation,
        "property_address": property_address,
        "property_postcode": property_postcode,
        "property_type": property_type,
        "year_built": int(year_built),
        "construction": construction,
        "num_storeys": int(num_storeys),
        "product_type": product_type,
        "sum_insured": float(sum_insured),
        "policy_start_date": policy_start_date,
        "claims_last_5_years": int(claims_last_5_years),
        "prior_claim_types": [
            item.strip()
            for item in prior_claim_types_str.split(",")
            if item.strip()
        ],
        "outstanding_claims": outstanding_claims,
        "broker_reference": broker_reference or None,
    }

    try:
        with st.spinner("Gathering findings from risk tools..."):
            findings = request_findings(payload)
        render_findings(findings)
        st.divider()
    except requests.exceptions.ConnectionError:
        st.error(
            f"Cannot reach the underwriting API at {API_URL}. "
            "Start it with: uvicorn api.main:app --port 8010"
        )
        return
    except (requests.exceptions.Timeout, RuntimeError) as exc:
        st.warning(f"Findings unavailable: {exc}")

    try:
        with st.spinner("Assessing risk..."):
            result = request_assessment(payload)
    except requests.exceptions.ConnectionError:
        st.error(
            f"Cannot reach the underwriting API at {API_URL}. "
            "Start it with: uvicorn api.main:app --port 8010"
        )
        return
    except requests.exceptions.Timeout:
        st.error(f"The assessment exceeded {API_TIMEOUT:.0f}s and timed out.")
        return
    except RuntimeError as exc:
        st.error(str(exc))
        return

    render_assessment_result(result)


def _queue_table_rows(messages: list[dict]) -> list[dict]:
    rows = []
    for message in messages:
        payload = message["payload"]
        submission = payload.get("submission", {})
        if not isinstance(submission, dict):
            submission = {}
        risk_flags = payload.get("risk_flags", [])
        if not isinstance(risk_flags, list):
            risk_flags = [risk_flags]

        rows.append(
            {
                "Submitted": _format_datetime(
                    payload.get("created_at_utc") or message["inserted_at"]
                ),
                "Reference": payload.get("broker_reference", "Not provided"),
                "Applicant": submission.get("applicant_name", "Not provided"),
                "Confidence": payload.get("confidence", ""),
                "Postcode": submission.get("property_postcode", ""),
                "Property": _humanize(submission.get("property_type")),
                "Cover": _humanize(submission.get("product_type")),
                "Sum insured": _format_currency(submission.get("sum_insured")),
                "Risk flags": len(risk_flags),
            }
        )
    return rows


def _message_label(message: dict, position: int) -> str:
    payload = message["payload"]
    submission = payload.get("submission", {})
    if not isinstance(submission, dict):
        submission = {}
    broker_reference = payload.get("broker_reference") or "No broker reference"
    applicant = submission.get("applicant_name") or "Applicant not provided"
    postcode = submission.get("property_postcode") or "No postcode"
    return f"{position + 1}. {broker_reference} · {applicant} · {postcode}"


def _render_case_details(decision: str, message: dict) -> None:
    if not message["valid_json"]:
        st.warning("Case details are unavailable for this record.")
        return

    payload = message["payload"]
    submission = payload.get("submission", {})
    if not isinstance(submission, dict):
        submission = {}

    broker_reference = payload.get("broker_reference") or "No broker reference"
    applicant_name = submission.get("applicant_name") or "Applicant not provided"
    confidence = _humanize(payload.get("confidence"))

    st.divider()
    st.subheader(broker_reference)
    submitted_at = payload.get("created_at_utc") or message["inserted_at"]
    st.caption(
        f"{applicant_name} · Submitted {_format_datetime(submitted_at)}"
    )

    if decision == "ACCEPT":
        st.success(f"Accepted · {confidence} confidence")
    elif decision == "DECLINE":
        st.error(f"Declined · {confidence} confidence")
    else:
        st.warning(f"Referred · {confidence} confidence")

    summary_columns = st.columns(4)
    summary_columns[0].metric("Decision", _humanize(decision))
    summary_columns[1].metric("Confidence", confidence)
    summary_columns[2].metric(
        "Sum insured", _format_currency(submission.get("sum_insured"))
    )
    summary_columns[3].metric(
        "Claims in 5 years",
        _display_value(submission.get("claims_last_5_years"), "0"),
    )

    st.subheader("Decision Rationale")
    st.write(payload.get("rationale") or "No rationale was provided.")

    if payload.get("refer_reason"):
        st.warning(f"Reason for referral: {payload['refer_reason']}")

    st.subheader("Risk Considerations")
    risk_flags = payload.get("risk_flags", [])
    if not isinstance(risk_flags, list):
        risk_flags = [risk_flags]
    if risk_flags:
        st.markdown("\n".join(f"- {_humanize(flag)}" for flag in risk_flags))
    else:
        st.success("No material risk flags identified")

    st.divider()
    applicant_column, property_column = st.columns(2)
    with applicant_column:
        st.subheader("Applicant")
        applicant_fields = st.columns(2)
        with applicant_fields[0]:
            _render_field("Name", submission.get("applicant_name"))
        with applicant_fields[1]:
            _render_field(
                "Date of birth", _format_date(submission.get("date_of_birth"))
            )
        _render_field("Occupation", submission.get("occupation"))

    with property_column:
        st.subheader("Property")
        _render_field("Address", submission.get("property_address"))
        property_fields = st.columns(2)
        with property_fields[0]:
            _render_field("Postcode", submission.get("property_postcode"))
            _render_field("Year built", submission.get("year_built"))
            _render_field("Storeys", submission.get("num_storeys"))
        with property_fields[1]:
            _render_field(
                "Property type", _humanize(submission.get("property_type"))
            )
            _render_field(
                "Construction", _humanize(submission.get("construction"))
            )

    coverage_column, claims_column = st.columns(2)
    with coverage_column:
        st.subheader("Coverage")
        coverage_fields = st.columns(2)
        with coverage_fields[0]:
            _render_field(
                "Product", _humanize(submission.get("product_type"))
            )
            _render_field(
                "Policy start", _format_date(submission.get("policy_start_date"))
            )
        with coverage_fields[1]:
            _render_field(
                "Sum insured", _format_currency(submission.get("sum_insured"))
            )
            _render_field(
                "Flood Re eligible", _yes_no(payload.get("flood_re_eligible"))
            )
        if payload.get("recommended_premium_loading") is not None:
            _render_field(
                "Recommended premium loading",
                f"{payload['recommended_premium_loading']}%",
            )

    with claims_column:
        st.subheader("Claims")
        claims_fields = st.columns(2)
        with claims_fields[0]:
            _render_field(
                "Claims in last 5 years",
                submission.get("claims_last_5_years", 0),
            )
        with claims_fields[1]:
            _render_field(
                "Outstanding claims",
                _yes_no(submission.get("outstanding_claims")),
            )
        prior_claim_types = submission.get("prior_claim_types", [])
        if isinstance(prior_claim_types, list):
            prior_claim_types = ", ".join(
                _humanize(claim_type) for claim_type in prior_claim_types
            )
        _render_field("Prior claim types", prior_claim_types or "None")
        _render_field("Special conditions", submission.get("special_conditions"))


def render_queue_tab(decision: str, snapshot: dict) -> None:
    messages = snapshot["messages"]

    if not messages:
        st.info("No visible messages")
        return

    st.dataframe(
        _queue_table_rows(messages),
        hide_index=True,
        width="stretch",
        column_config={
            "Submitted": st.column_config.TextColumn(width="medium"),
            "Reference": st.column_config.TextColumn(width="medium"),
            "Applicant": st.column_config.TextColumn(width="medium"),
            "Confidence": st.column_config.TextColumn(width="small"),
            "Postcode": st.column_config.TextColumn(width="small"),
            "Property": st.column_config.TextColumn(width="small"),
            "Cover": st.column_config.TextColumn(width="small"),
            "Sum insured": st.column_config.TextColumn(width="small"),
            "Risk flags": st.column_config.NumberColumn(width="small"),
        },
    )

    selected_index = st.selectbox(
        "Review case",
        options=range(len(messages)),
        format_func=lambda index: _message_label(messages[index], index),
        key=f"inspect-{decision.lower()}",
    )
    selected = messages[selected_index]
    _render_case_details(decision, selected)


def render_queue_page() -> None:
    heading_col, action_col = st.columns([5, 1], vertical_alignment="bottom")
    with heading_col:
        st.title("Case Review")
    with action_col:
        if st.button(
            "Refresh",
            icon=":material/refresh:",
            help="Refresh queue counts and visible messages",
            use_container_width=True,
        ):
            fetch_queue_snapshot.clear()

    snapshots = {}
    errors = {}
    with st.spinner("Loading decision queues..."):
        for decision, queue_name in DECISION_QUEUES.items():
            try:
                snapshots[decision] = fetch_queue_snapshot(queue_name)
            except Exception as exc:
                errors[decision] = exc

    metric_columns = st.columns(3)
    for column, (decision, queue_name) in zip(
        metric_columns, DECISION_QUEUES.items()
    ):
        snapshot = snapshots.get(decision)
        count = snapshot["approximate_count"] if snapshot else "-"
        column.metric(decision, count)

    refresh_times = [
        snapshot["fetched_at"]
        for snapshot in snapshots.values()
        if snapshot.get("fetched_at")
    ]
    if refresh_times:
        st.caption(f"Last refreshed: {max(refresh_times)}")

    tabs = st.tabs(list(DECISION_QUEUES))
    for tab, (decision, queue_name) in zip(tabs, DECISION_QUEUES.items()):
        with tab:
            if decision in errors:
                exc = errors[decision]
                logger.warning(
                    "Unable to read %s decision cases: %s",
                    decision,
                    exc,
                    exc_info=True,
                )
                status_code = getattr(exc, "status_code", None)
                if status_code in {401, 403}:
                    st.error("You do not have permission to view these cases.")
                elif status_code == 404:
                    st.error("This case list is not available yet.")
                else:
                    st.error("These cases are temporarily unavailable.")
                continue
            render_queue_tab(decision, snapshots[decision])


def main() -> None:
    assessment_page = st.Page(
        render_assessment_page,
        title="New Assessment",
        icon=":material/assignment:",
        default=True,
    )
    queue_page = st.Page(
        render_queue_page,
        title="Case Review",
        icon=":material/queue:",
        url_path="case-review",
    )

    navigation = st.navigation([assessment_page, queue_page], expanded=True)
    render_service_status()
    navigation.run()


if __name__ == "__main__":
    main()
