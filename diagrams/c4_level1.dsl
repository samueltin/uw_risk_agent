workspace "Underwriting Risk Assessment Agent" "C4 Level 1 — System Context" {

    model {

        # ----------------------------------------------------------------
        # People
        # ----------------------------------------------------------------

        broker = person "Broker" {
            description "Insurance broker submitting property risk applications on behalf of clients."
            tags "Person"
        }

        underwriter = person "Senior Underwriter" {
            description "Human underwriter who reviews referred cases escalated by the agent."
            tags "Person"
        }

        # ----------------------------------------------------------------
        # The system
        # ----------------------------------------------------------------

        uwAgent = softwareSystem "Underwriting Risk Assessment Agent" {
            description "Agentic AI system that assesses UK property insurance submissions. Uses MCP tools and RAG to autonomously gather risk signals and produce ACCEPT / REFER / DECLINE decisions."
            tags "Internal"
        }

        # ----------------------------------------------------------------
        # External systems
        # ----------------------------------------------------------------

        azureFoundry = softwareSystem "Azure AI Foundry" {
            description "Microsoft cloud platform hosting the GPT-4o agent loop, thread and run management via the Agent Framework."
            tags "Azure"
        }

        azureOpenAI = softwareSystem "Azure OpenAI" {
            description "Provides GPT-4o (GlobalStandard) for agent reasoning and text-embedding-3-small for guideline indexing."
            tags "Azure"
        }

        azureSearch = softwareSystem "Azure AI Search" {
            description "Vector search index containing chunked and embedded UK property underwriting guidelines. Queried via RAG at inference time."
            tags "Azure"
        }

        eaFloodAPI = softwareSystem "Environment Agency Flood API" {
            description "UK government API returning active flood warnings and zone classifications for a given postcode. environment.data.gov.uk"
            tags "External"
        }

        policeAPI = softwareSystem "data.police.uk Crime API" {
            description "UK government API returning street-level crime data for a given location. Used to calculate a calibrated property crime index."
            tags "External"
        }

        cue = softwareSystem "Claims & Underwriting Exchange (CUE)" {
            description "Industry claims database for verifying prior claims history. Mocked in current prototype; production integration via Insurance Fraud Bureau."
            tags "External"
        }

        # ----------------------------------------------------------------
        # Relationships
        # ----------------------------------------------------------------

        broker      -> uwAgent     "Submits broker submission via Streamlit UI"
        uwAgent     -> underwriter "Escalates REFER decisions to human review queue"

        uwAgent     -> azureFoundry "Creates agent, thread, message, run via azure-ai-agents SDK"
        azureFoundry -> azureOpenAI "Routes inference requests to GPT-4o deployment"

        uwAgent     -> azureSearch  "Retrieves relevant UW guideline chunks via semantic hybrid search"
        azureOpenAI -> azureSearch  "Generates embeddings for vector search (text-embedding-3-small)"

        uwAgent     -> eaFloodAPI   "Calls flood zone lookup tool via MCP server (Environment Agency REST API)"
        uwAgent     -> policeAPI    "Calls crime index tool via MCP server (data.police.uk REST API)"
        uwAgent     -> cue          "Calls claims history tool via MCP server (mock in prototype)"

    }

    views {

        systemContext uwAgent "SystemContext" {
            include *
            autoLayout lr
            title "Underwriting Risk Assessment Agent — C4 Level 1 System Context"
            description "Shows the agent system, its users, and the external systems it interacts with."
        }

        styles {

            element "Person" {
                shape Person
                background "#1168BD"
                color "#ffffff"
                fontSize 16
            }

            element "Internal" {
                background "#1168BD"
                color "#ffffff"
                shape RoundedBox
                fontSize 16
            }

            element "Azure" {
                background "#0078D4"
                color "#ffffff"
                shape RoundedBox
                fontSize 14
            }

            element "External" {
                background "#999999"
                color "#ffffff"
                shape RoundedBox
                fontSize 14
            }

            relationship "Relationship" {
                fontSize 13
                color "#707070"
            }
        }

        themes default
    }

}
