# Outputs

output "resource_group_name" {
  description = "The name of the deployed Resource Group."
  value       = azurerm_resource_group.main.name
}

output "ai_search_service_name" {
  description = "The name of the deployed Azure AI Search service."
  value       = azurerm_search_service.main.name
}

output "openai_account_name" {
  description = "The name of the deployed Azure OpenAI account."
  value       = azurerm_cognitive_account.openai.name
}

output "openai_account_endpoint" {
  description = "The endpoint URL for the Azure OpenAI account."
  value       = azurerm_cognitive_account.openai.endpoint
}

output "gpt4_deployment_name" {
  description = "The name of the GPT-4.1 deployment."
  value       = azurerm_cognitive_deployment.gpt4.name
}

output "text_embedding_deployment_name" {
  description = "The name of the text-embedding-3-small deployment."
  value       = azurerm_cognitive_deployment.text_embedding.name
}

output "ai_hub_name" {
  description = "The name of the Azure AI Foundry Hub."
  value       = azurerm_ai_foundry.hub.name
}

output "ai_project_name" {
  description = "The name of the Azure AI Foundry Project."
  value       = azurerm_ai_foundry_project.main.name
}

output "ai_project_id" {
  description = "AI Foundry Project resource ID"
  value       = azurerm_ai_foundry_project.main.id
}

output "ai_hub_discovery_url" {
  description = "AI Foundry Hub discovery URL — endpoint base can be derived from this"
  value       = azurerm_ai_foundry.hub.discovery_url
}

output "ai_search_endpoint" {
  description = "AZURE_SEARCH_ENDPOINT — paste into .env"
  value       = "https://${azurerm_search_service.main.name}.search.windows.net"
}

output "ai_search_admin_key" {
  description = "AZURE_SEARCH_ADMIN_KEY — paste into .env (sensitive)"
  value       = azurerm_search_service.main.primary_key
  sensitive   = true
}

output "openai_api_key" {
  description = "AZURE_OPENAI_API_KEY — paste into .env (sensitive)"
  value       = azurerm_cognitive_account.openai.primary_access_key
  sensitive   = true
}

# Convenience block — run: terraform output -raw env_file_block >> ../.env
output "env_file_block" {
  description = "Ready-to-paste .env block. AZURE_AI_PROJECT_ENDPOINT must be filled manually from the Azure portal."
  value       = <<-ENV
# Get AZURE_AI_PROJECT_ENDPOINT from:
# Azure AI Foundry portal → your project → Overview → Project details
AZURE_AI_PROJECT_ENDPOINT=https://<account>.services.ai.azure.com/api/projects/${azurerm_ai_foundry_project.main.name}
AZURE_OPENAI_ENDPOINT=${azurerm_cognitive_account.openai.endpoint}
AZURE_OPENAI_MODEL=${azurerm_cognitive_deployment.gpt4.name}
AZURE_EMBED_DEPLOYMENT=${azurerm_cognitive_deployment.text_embedding.name}
AZURE_SEARCH_ENDPOINT=https://${azurerm_search_service.main.name}.search.windows.net
AZURE_SEARCH_INDEX_NAME=uw-guidelines
MCP_RISK_SERVER_URL=http://127.0.0.1:8001/mcp
  ENV
  sensitive = false
}
