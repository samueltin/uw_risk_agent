# Resource Group
resource "azurerm_resource_group" "main" {
  name     = var.resource_group_name
  location = var.location
}

# Azure AI Search Service
resource "azurerm_search_service" "main" {
  name                = var.ai_search_service_name
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  sku                 = "free"  # Free tier: 3 indexes, 50MB, no idle charge
  replica_count       = 1
  partition_count     = 1
}

# Azure OpenAI Account
# Note: deployed to openai_location (East US) as GPT-4.1 availability
# varies by region. All other resources deploy to location (UK South).
resource "azurerm_cognitive_account" "openai" {
  name                = var.openai_account_name
  resource_group_name = azurerm_resource_group.main.name
  location            = var.openai_location
  kind                = "OpenAI"
  sku_name            = "S0"
}

# Azure OpenAI Deployment for text-embedding-3-small
# Used by knowledge_base/ingest.py to embed UW guidelines
resource "azurerm_cognitive_deployment" "text_embedding" {
  name                 = var.text_embedding_deployment_name
  cognitive_account_id = azurerm_cognitive_account.openai.id

  model {
    format  = "OpenAI"
    name    = "text-embedding-3-small"
    version = "1"
  }

  sku {
    name = "GlobalStandard"
  }
}

# Azure OpenAI Deployment for GPT-4.1
# Used by the agentic orchestrator loop
resource "azurerm_cognitive_deployment" "gpt4" {
  name                 = var.gpt4_deployment_name
  cognitive_account_id = azurerm_cognitive_account.openai.id

  model {
    format  = "OpenAI"
    name    = "gpt-4.1"
    version = var.gpt4_model_version
  }

  sku {
    name     = "GlobalStandard"
    capacity = 10  # 10K tokens per minute — sufficient for demo
  }
}

# Storage Account — required by AI Foundry Hub
resource "azurerm_storage_account" "main" {
  name                     = var.storage_account_name
  resource_group_name      = azurerm_resource_group.main.name
  location                 = azurerm_resource_group.main.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
}

# Key Vault — required by AI Foundry Hub
data "azurerm_client_config" "current" {}

resource "azurerm_key_vault" "main" {
  name                = var.key_vault_name
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  tenant_id           = data.azurerm_client_config.current.tenant_id
  sku_name            = "standard"

  access_policy {
    tenant_id = data.azurerm_client_config.current.tenant_id
    object_id = data.azurerm_client_config.current.object_id

    secret_permissions = ["Get", "List", "Set", "Delete", "Purge"]
    key_permissions    = ["Get", "List", "Create", "Delete", "Purge"]
  }
}

# Azure AI Foundry Hub
# Required to use AIProjectClient (the agentic orchestrator)
resource "azurerm_ai_foundry" "hub" {
  name                = var.ai_hub_name
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  storage_account_id  = azurerm_storage_account.main.id
  key_vault_id        = azurerm_key_vault.main.id

  identity {
    type = "SystemAssigned"
  }
}

# Azure AI Foundry Project
# AZURE_AI_PROJECT_ENDPOINT is derived from this resource
resource "azurerm_ai_foundry_project" "main" {
  name               = var.ai_project_name
  location           = azurerm_resource_group.main.location
  ai_services_hub_id = azurerm_ai_foundry.hub.id

  identity {
    type = "SystemAssigned"
  }
}
