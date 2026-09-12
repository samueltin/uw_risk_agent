"""
Underwriting Risk Assessment — Streamlit UI
--------------------------------------------
Broker-facing submission form. Posts the submission to the underwriting
API service and displays the decision with full rationale and risk flags.

The UI holds no orchestrator logic and imports no agent SDK — it speaks
HTTP to api/main.py, which decides which orchestrator runs.

    Terminal 1:  python mcp_servers/risk_server_v4.py
    Terminal 2:  uvicorn api.main:app --port 8010
    Terminal 3:  streamlit run app.py
"""

import os
import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

API_URL = os.environ.get("UW_API_URL", "http://127.0.0.1:8010")

# Agent runs take tens of seconds — the tool loop plus model latency.
API_TIMEOUT = float(os.environ.get("UW_API_TIMEOUT", "300"))


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

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Underwriting Risk Assessment",
    page_icon="🏠",
    layout="wide",
)

st.title("🏠 Underwriting Risk Assessment Agent")
st.caption("Agentic underwriting over MCP tools and a RAG guidelines index")

# ---------------------------------------------------------------------------
# Sidebar — read-only service status
#
# The LLM is chosen by the service, via UW_LLM_PROVIDER in .env. The UI
# only reports which one is active; it never selects one.
# ---------------------------------------------------------------------------

@st.cache_data(ttl=30)
def fetch_health() -> dict:
    """Ask the API for its status, provider and model."""
    response = requests.get(f"{API_URL}/health", timeout=10)
    response.raise_for_status()
    return response.json()


with st.sidebar:
    st.subheader("Service")
    st.caption(f"API: `{API_URL}`")
    try:
        health = fetch_health()
    except Exception as e:
        st.error("API unreachable")
        st.caption(f"{type(e).__name__}: {e}")
        st.code("uvicorn api.main:app --port 8010", language="bash")
    else:
        st.success(f"Connected · `{health['provider']}`")
        st.caption(f"Model: `{health['model']}`")
        if health.get("detail"):
            st.warning(health["detail"])

        st.caption("Set `UW_LLM_PROVIDER` in .env to change, then restart the API.")

# ---------------------------------------------------------------------------
# Submission form
# ---------------------------------------------------------------------------

with st.form("submission_form"):
    st.subheader("Applicant")
    col1, col2, col3 = st.columns(3)
    applicant_name   = col1.text_input("Full name", value="Jane Smith")
    date_of_birth    = col2.text_input("Date of birth (YYYY-MM-DD)", value="1978-06-15")
    occupation       = col3.text_input("Occupation", value="Teacher")

    st.subheader("Risk Location")
    col4, col5 = st.columns(2)
    property_address  = col4.text_input("Address", value="12 Riverside Close, Bristol")
    property_postcode = col5.text_input("Postcode", value="BS1 4DJ")

    col6, col7, col8, col9 = st.columns(4)
    property_type = col6.selectbox("Property type",
                                   ["detached", "semi", "flat", "commercial"])
    year_built    = col7.number_input("Year built", min_value=1600,
                                      max_value=2026, value=1912)
    construction  = col8.selectbox("Construction", ["brick", "timber", "concrete"])
    num_storeys   = col9.number_input("Storeys", min_value=1, max_value=20, value=2)

    st.subheader("Coverage")
    col10, col11, col12 = st.columns(3)
    product_type      = col10.selectbox("Product", ["buildings", "contents", "combined"])
    sum_insured       = col11.number_input("Sum insured (£)", min_value=10000,
                                           max_value=10000000, value=425000, step=5000)
    policy_start_date = col12.text_input("Start date (YYYY-MM-DD)", value="2026-05-01")

    st.subheader("Claims History")
    col13, col14, col15 = st.columns(3)
    claims_last_5_years = col13.number_input("Claims in last 5 years",
                                              min_value=0, max_value=20, value=2)
    prior_claim_types_str = col14.text_input(
        "Claim types (comma-separated)",
        value="escape_of_water, subsidence"
    )
    outstanding_claims = col15.checkbox("Outstanding claims?", value=False)

    broker_reference = st.text_input("Broker reference (optional)", value="BRK-2026-00142")

    submitted = st.form_submit_button("▶ Run Assessment", type="primary",
                                      use_container_width=True)

# ---------------------------------------------------------------------------
# Run assessment and display result
# ---------------------------------------------------------------------------

if submitted:
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
        "prior_claim_types": [t.strip() for t in prior_claim_types_str.split(",") if t.strip()],
        "outstanding_claims": outstanding_claims,
        "broker_reference": broker_reference or None,
    }
    try:
        with st.spinner("Agent assessing risk — calling tools in loop..."):
            result = request_assessment(payload)
    except requests.exceptions.ConnectionError:
        st.error(
            f"Cannot reach the underwriting API at {API_URL}. "
            "Start it with:  uvicorn api.main:app --port 8010"
        )
        st.stop()
    except requests.exceptions.Timeout:
        st.error(f"The assessment exceeded {API_TIMEOUT:.0f}s and timed out.")
        st.stop()
    except RuntimeError as e:
        st.error(str(e))
        st.stop()

    # Decision banner
    if result["decision"] == "ACCEPT":
        st.success(f"✅ **ACCEPT** — Confidence: {result['confidence']}")
    elif result["decision"] == "REFER":
        st.warning(f"⚠️ **REFER** — Confidence: {result['confidence']}")
    else:
        st.error(f"❌ **DECLINE** — Confidence: {result['confidence']}")

    # Detail columns
    col_a, col_b = st.columns(2)

    with col_a:
        st.subheader("Rationale")
        st.write(result["rationale"])

        if result.get("refer_reason"):
            st.info(f"**Refer reason:** {result['refer_reason']}")

        if result.get("recommended_premium_loading"):
            st.metric("Premium loading", f"+{result['recommended_premium_loading']:.1f}%")

        st.metric("Flood Re eligible", "Yes" if result["flood_re_eligible"] else "No")
        st.metric("Processing time", f"{result['processing_time_ms']}ms")

    with col_b:
        st.subheader("Risk Flags")
        if result["risk_flags"]:
            for flag in result["risk_flags"]:
                severity = "🔴" if any(x in flag for x in ["HIGH", "3B", "DECLINE", "ERROR"]) \
                           else "🟡" if any(x in flag for x in ["REFER", "ANOMALY", "TIMBER"]) \
                           else "🟢"
                st.write(f"{severity} `{flag}`")
        else:
            st.write("No risk flags raised.")

    # Audit trail
    with st.expander("Raw agent output (audit trail)"):
        st.code(result["raw_agent_output"], language="json")
