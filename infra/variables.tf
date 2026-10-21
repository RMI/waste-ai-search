variable "subscription_id" {
  description = "RMI-PROJECT-WASTEMAP-SUB."
  type        = string
  default     = "5420f2da-a885-430a-87b4-7934a1ffbcd7"
}

variable "resource_group_name" {
  description = "An existing resource group: our roles can't create one. The data pipeline's, beside its translator."
  type        = string
  default     = "RMI-WASTEMAP-PIPELINE-RG"
}

variable "location" {
  description = "Where the Foundry account runs. gpt-5.4-mini isn't offered in westus2, where the rest of WasteMAP is."
  type        = string
  default     = "eastus"
}

variable "account_name" {
  description = "The Foundry (AI Services) account."
  type        = string
  default     = "wastemap-ai-search"
}

variable "custom_subdomain" {
  description = "The account's endpoint name, unique across Azure; suffixed like the state account."
  type        = string
  default     = "wastemap-ai-search-5420f2"
}

variable "project_name" {
  description = "The Foundry project the agent lives in."
  type        = string
  default     = "waste-ai-search"
}

variable "model_name" {
  description = "The model the agent runs on, as in batch 1."
  type        = string
  default     = "gpt-5.4-mini"
}

variable "model_version" {
  description = "Pinned, so a run's model doesn't change under it. Retires 2027-09-21."
  type        = string
  default     = "2026-03-17"
}

variable "model_capacity" {
  description = "Rate limit, in thousands of tokens a minute (GlobalStandard is billed per token, not by this). Batch 1's deployment throttled at more than 10 workers; the subscription's quota is 10,000."
  type        = number
  default     = 2000
}

variable "tags" {
  description = "Tags on everything this creates."
  type        = map(string)
  default = {
    project    = "wastemap"
    component  = "waste-ai-search"
    managed_by = "terraform: RMI/waste-ai-search/infra"
  }
}
