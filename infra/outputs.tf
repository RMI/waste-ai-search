output "project_endpoint" {
  description = "AZURE_AI_PROJECT_ENDPOINT in .env."
  value       = "https://${var.custom_subdomain}.services.ai.azure.com/api/projects/${azapi_resource.project.name}"
}

output "model_deployment" {
  description = "The model deployment the agent is created on."
  value       = azapi_resource.model.name
}

output "comparison_model_deployment" {
  description = "The second model's deployment, for a second agent to compare with."
  value       = one(azapi_resource.comparison_model[*].name)
}

output "batch_model_deployment" {
  description = "The model batch 3 ran on."
  value       = azapi_resource.batch_model.name
}

output "second_pass_model_deployment" {
  description = "The model batch 3's second pass ran on."
  value       = azapi_resource.second_pass_model.name
}

output "account_endpoint" {
  description = "The Foundry account's endpoint."
  value       = azapi_resource.foundry.output.properties.endpoint
}
