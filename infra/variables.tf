# Variables

variable "subscription_id" {
  description = "Azure subscription ID."
  type        = string
}

variable "resource_group_name" {
  description = "The name of the resource group."
  type        = string
  default     = "uw-risk-agent-rg"
}

variable "location" {
  description = "The Azure region to deploy resources."
  type        = string
  default     = "UK South"
}

variable "openai_location" {
  description = "Azure region for OpenAI resources. GPT-4.1 availability varies by region."
  type        = string
  default     = "East US"
}

variable "openai_account_name" {
  description = "The name of the Azure OpenAI account."
  type        = string
  default     = "uw-risk-agent-openai"
}

variable "gpt4_deployment_name" {
  description = "The name for the GPT-4.1 model deployment."
  type        = string
  default     = "gpt-4.1"
}

variable "gpt4_model_version" {
  description = "The version of the GPT-4.1 model."
  type        = string
  default     = "2025-04-14"
}

variable "text_embedding_deployment_name" {
  description = "The name for the text-embedding-3-small deployment."
  type        = string
  default     = "text-embedding-3-small"
}

variable "ai_search_service_name" {
  description = "The name of the Azure AI Search service."
  type        = string
  default     = "uw-risk-agent-search"
}

variable "ai_search_index_name" {
  description = "The name of the Azure AI Search index for UW guidelines."
  type        = string
  default     = "uw-guidelines"
}

variable "storage_account_name" {
  description = "The name of the Storage Account required by AI Foundry Hub."
  type        = string
  default     = "uwagentstorage"
}

variable "key_vault_name" {
  description = "The name of the Key Vault required by AI Foundry Hub."
  type        = string
  default     = "uw-risk-agent-kv"
}

variable "ai_hub_name" {
  description = "The name of the Azure AI Foundry Hub."
  type        = string
  default     = "uw-risk-agent-hub"
}

variable "ai_project_name" {
  description = "The name of the Azure AI Foundry Project."
  type        = string
  default     = "uw-risk-agent-project"
}
