output "project_endpoint" {
  description = "AZURE_AI_PROJECT_ENDPOINT in .env."
  value       = "https://${var.custom_subdomain}.services.ai.azure.com/api/projects/${azapi_resource.project.name}"
}

output "model_deployment" {
  description = "The model deployment the agent is created on."
  value       = azapi_resource.model.name
}

output "account_endpoint" {
  description = "The Foundry account's endpoint."
  value       = azapi_resource.foundry.output.properties.endpoint
}
