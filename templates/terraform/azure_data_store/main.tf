/*
Copyright 2023 Monte Carlo Data, Inc.

The Software contained herein (the "Software") is the intellectual property of Monte Carlo Data, Inc. ("Licensor"),
and Licensor retains all intellectual property rights in the Software, including any and all derivatives, changes and
improvements thereto. Only customers who have entered into a commercial agreement with Licensor for use or
purchase of the Software ("Licensee") are licensed or otherwise authorized to use the Software, and any Licensee
agrees that it obtains no copyright or other intellectual property rights to the Software, except for the license
expressly granted below or in accordance with the terms of their commercial agreement with Licensor (the
"Agreement"). Subject to the terms and conditions of the Agreement, Licensor grants Licensee a non-exclusive,
non-transferable, non-sublicensable, revocable, limited right and license to use the Software, in each case solely
internally within Licensee's organization for non-commercial purposes and only in connection with the service
provided by Licensor pursuant to the Agreement, and in object code form only. Without Licensor's express prior
written consent, Licensee may not, directly or indirectly, (i) distribute the Software, any portion thereof, or any
modifications, enhancements, or derivative works of any of the foregoing (collectively, the "Derivatives") to any
third party, (ii) license, market, sell, offer for sale or otherwise attempt to commercialize any Software, Derivatives,
or portions thereof, (iii) use the Software, Derivatives, or any portion thereof for the benefit of any third party, (iv)
use the Software, Derivatives, or any portion thereof in any manner or with respect to any commercial activity
which competes, or is reasonably likely to compete, with any business that Licensor conducts, proposes to conduct
or demonstrably anticipates conducting, at any time; or (v) seek any patent or other intellectual property rights or
protections over or in connection with any Software of Derivatives.
*/

/*
Sample Terraform config file to create an Azure Storage Account and Container for the cloud with customer-hosted
object storage deployment model on Azure. Additional details and options can be found here:
https://docs.getmontecarlo.com/docs/create-and-register-an-azure-blob-data-store

Note that this will persist the storage account connection string in the remote state used by Terraform.
Please take appropriate measures to protect your remote state.

Usage example (requires Terraform and the Azure CLI):
  az login
  terraform init
  terraform apply

Inputs:
  resource_group_name - The Azure Resource Group to deploy into [REQUIRED].
  location            - The Azure region to deploy into (default: "East US").

Outputs:
  container_name    - The generated storage container name.
  connection_string - The connection string for Monte Carlo to access the container.
                      Can retrieve via `terraform output -raw connection_string`

These can be used when registering: https://docs.getmontecarlo.com/docs/create-and-register-an-azure-blob-data-store
*/

terraform {
  required_version = ">= 1.3"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }
}

provider "azurerm" {
  features {}
}

variable "resource_group_name" {
  description = "The Azure Resource Group to deploy into."
  type        = string
}

variable "location" {
  description = "The Azure region to deploy into."
  type        = string
  default     = "East US"
}

resource "random_id" "this" {
  byte_length = 4
}

resource "azurerm_storage_account" "this" {
  name                              = "mcdstore${random_id.this.hex}"
  resource_group_name               = var.resource_group_name
  location                          = var.location
  account_tier                      = "Standard"
  account_replication_type          = "LRS"
  https_traffic_only_enabled        = true
  allow_nested_items_to_be_public   = false
  min_tls_version                   = "TLS1_2"
  cross_tenant_replication_enabled  = false
  infrastructure_encryption_enabled = true

  tags = {
    "MonteCarloData" = ""
  }
}

resource "azurerm_storage_management_policy" "this" {
  storage_account_id = azurerm_storage_account.this.id

  rule {
    name    = "custom-sql-output-samples-expiration"
    enabled = true

    filters {
      prefix_match = ["custom-sql-output-samples/"]
      blob_types   = ["blockBlob"]
    }

    actions {
      base_blob {
        delete_after_days_since_modification_greater_than = 90
      }
    }
  }

  rule {
    name    = "rca-expiration"
    enabled = true

    filters {
      prefix_match = ["rca"]
      blob_types   = ["blockBlob"]
    }

    actions {
      base_blob {
        delete_after_days_since_modification_greater_than = 90
      }
    }
  }

  rule {
    name    = "idempotent-expiration"
    enabled = true

    filters {
      prefix_match = ["idempotent"]
      blob_types   = ["blockBlob"]
    }

    actions {
      base_blob {
        delete_after_days_since_modification_greater_than = 90
      }
    }
  }
}

resource "azurerm_storage_container" "this" {
  name                  = "mcd-store"
  storage_account_id    = azurerm_storage_account.this.id
  container_access_type = "private"
}

output "container_name" {
  description = "Name of the storage container. To be used in registering."
  value       = azurerm_storage_container.this.name
}

output "connection_string" {
  description = "Connection string for the storage account. To be used in registering."
  value       = azurerm_storage_account.this.primary_connection_string
  sensitive   = true
}
