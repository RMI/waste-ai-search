terraform {
  required_version = ">= 1.6.0"

  required_providers {
    azapi = {
      source  = "Azure/azapi"
      version = "~> 2.0"
    }
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.3"
    }
  }

  # The same state account as RMI/terraform's wastemap module, under a key of its own.
  backend "azurerm" {
    resource_group_name  = "RMI-WASTEMAP-PIPELINE-RG"
    storage_account_name = "wmaptfstate5420f2"
    container_name       = "tfstate"
    key                  = "waste-ai-search.tfstate"
    use_azuread_auth     = true
  }
}

# Everything lives in the new subscription. Resource providers are registered by IT, as for the
# wastemap module, so Terraform doesn't try to.
provider "azurerm" {
  subscription_id                 = var.subscription_id
  resource_provider_registrations = "none"
  features {}
}

provider "azapi" {
  subscription_id = var.subscription_id
}
