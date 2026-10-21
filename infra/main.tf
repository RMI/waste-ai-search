# The AI search's Foundry: an account, a project and a model deployment. The agent itself, a prompt
# and the web search tool on that model, is created through the Foundry SDK by
# scripts/create_foundry_agent.py, since it isn't an Azure resource.
#
# AzAPI, not AzureRM: Microsoft documents AzAPI as covering everything the new Foundry projects
# need, and a project made with AzureRM's resources has been reported unusable by the agent
# service. No Bing resource is needed: the agent's web search tool (WebSearchTool) runs on Bing
# without one; a connection is only for Bing Custom Search.

data "azurerm_resource_group" "pipeline" {
  name = var.resource_group_name
}

resource "azapi_resource" "foundry" {
  type      = "Microsoft.CognitiveServices/accounts@2025-06-01"
  name      = var.account_name
  parent_id = data.azurerm_resource_group.pipeline.id
  location  = var.location
  tags      = var.tags

  identity {
    type = "SystemAssigned"
  }

  body = {
    kind = "AIServices"
    sku = {
      name = "S0"
    }
    properties = {
      allowProjectManagement = true
      customSubDomainName    = var.custom_subdomain
      publicNetworkAccess    = "Enabled"
      # Entra ID only: the search signs in as whoever runs it (`az login`), and no key can leak.
      disableLocalAuth = true
    }
  }

  response_export_values = ["properties.endpoint"]
}

resource "azapi_resource" "model" {
  type      = "Microsoft.CognitiveServices/accounts/deployments@2025-06-01"
  name      = var.model_name
  parent_id = azapi_resource.foundry.id

  body = {
    sku = {
      name     = "GlobalStandard"
      capacity = var.model_capacity
    }
    properties = {
      model = {
        format  = "OpenAI"
        name    = var.model_name
        version = var.model_version
      }
      versionUpgradeOption = "NoAutoUpgrade"
    }
  }
}

resource "azapi_resource" "project" {
  type      = "Microsoft.CognitiveServices/accounts/projects@2025-06-01"
  name      = var.project_name
  parent_id = azapi_resource.foundry.id
  location  = var.location
  tags      = var.tags

  identity {
    type = "SystemAssigned"
  }

  body = {
    properties = {
      displayName = "WasteMAP AI search"
      description = "Searches the web for missing WasteMAP facility data (RMI/waste-ai-search)."
    }
  }

  # One change to an account at a time: Azure refuses a second while the first is in progress.
  depends_on = [azapi_resource.model]
}
