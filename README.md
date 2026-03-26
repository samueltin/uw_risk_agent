# 🏠 Underwriting Risk Assessment Agent

An agentic AI system for UK property insurance underwriting, built with
**Microsoft Agent Framework**, **Azure OpenAI**, **MCP tools**, and **RAG**.

---

## Why this is a true agent (not a pipeline)

> "An LLM agent runs tools in a loop to achieve a goal." — Simon Willison

Most LLM "agent" systems are actually deterministic pipelines — Python code
calls LLM A, then LLM B, then LLM C in a fixed sequence. The LLM only does
text generation; Python controls the flow.

This project is different. **The LLM controls the flow.** It receives the
broker submission and a set of tools, then decides:
- Which tools to call
- In what order
- Whether to call more tools based on what it finds
- When it has enough evidence to produce a final decision

Microsoft Agent Framework's `create_and_process_run()` implements the loop:
```
LLM thinks → calls a tool → sees result → LLM thinks again → ...
→ LLM satisfied → returns final decision
```

For a high-risk submission (Zone 3b flood, 3+ claims), the LLM might call
`get_flood_zone`, immediately call `search_uw_guidelines("Zone 3b mandatory
exclusions")`, and return DECLINE — never bothering with crime or validation.
For a borderline case it might call all five tools and still return REFER.
The pipeline approach would always run every step regardless.

---

## Architecture

```
Broker submission
       │
       ▼
┌─────────────────────────────────────────────┐
│  Azure AI Foundry — Agent Loop              │
│                                             │
│  LLM (GPT-4.1)                              │
│   ├── validate_submission()   ← MCP tool    │
│   ├── get_flood_zone()        ← MCP tool    │
│   ├── get_crime_index()       ← MCP tool    │
│   ├── get_claims_history()    ← MCP tool    │
│   └── search_uw_guidelines()  ← RAG (AI Search) │
│                                             │
│  Loop until goal achieved                   │
└─────────────────────────────────────────────┘
       │
       ▼
ACCEPT / REFER / DECLINE + rationale + flags
       │
    REFER? → Human review queue
```

---

## Tech stack

| Component | Technology |
|---|---|
| Agent framework | Microsoft Agent Framework (azure-ai-projects) |
| LLM | Azure OpenAI GPT-4.1 (primary) |
| Tool protocol | MCP (Model Context Protocol) |
| Knowledge base | Azure AI Search (RAG) |
| Embeddings | Azure OpenAI text-embedding-3-small |
| UI | Streamlit |
| Auth | Azure DefaultAzureCredential (Entra ID) |

---

## Local testing (Ollama)

This prototype has been tested locally with Ollama using `qwen2.5:14b` as a drop-in
LLM for the agent loop.

Prereqs:
- Ollama installed and running locally
- Model pulled: `ollama pull qwen2.5:14b`

---

## Project structure

```
.
├── orchestrator.py            # Agentic loop — Azure OpenAI entry point
├── ollama_orchestrator.py     # Agentic loop — local Ollama entry point
├── app.py                     # Streamlit UI
├── models/
│   ├── submission.py          # Broker submission dataclass
│   └── decision.py            # Underwriting decision dataclass
├── mcp_servers/
│   ├── risk_server.py         # MCP server: baseline risk tools
│   ├── risk_server_v2.py      # MCP server: variant implementation
│   └── risk_server_v3.py      # MCP server: variant implementation
├── knowledge_base/
│   └── ingest.py              # Index UW guidelines into Azure AI Search
│   └── uw_guidelines.md       # Sample underwriting guidelines corpus
├── infra/                     # Terraform for Azure resources
├── env.example
├── requirements.txt
├── tests/
│   └── test_server.py
└── README.md
```

---

## Getting started

### 1. Clone and install

```bash
git clone https://github.com/samueltin/uw_risk_agent.git
cd uw_risk_agent
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Fill in AZURE_AI_PROJECT_ENDPOINT and AZURE_SEARCH_CONNECTION_ID
```

### 3. Start the MCP server

```bash
python mcp_servers/risk_server_v3.py
```

### 4. Run smoke test

```bash
python orchestrator.py
```

### 5. Launch UI

```bash
streamlit run app.py
```

---

## Key concepts demonstrated

- **Agentic loop** — LLM controls tool call sequence, not Python
- **MCP tools** — all risk lookups served over Model Context Protocol
- **RAG** — underwriting guidelines retrieved via Azure AI Search
- **Human-in-the-loop** — REFER cases escalated to review queue
- **Audit trail** — full raw agent output retained on every decision
- **Regulated sector patterns** — Flood Re eligibility, UK flood zones, FCA-aligned decision logging
