/*
Copyright 2023 Monte Carlo Data, Inc.

The Software contained herein (the “Software”) is the intellectual property of Monte Carlo Data, Inc. (“Licensor”),
and Licensor retains all intellectual property rights in the Software, including any and all derivatives, changes and
improvements thereto. Only customers who have entered into a commercial agreement with Licensor for use or
purchase of the Software (“Licensee”) are licensed or otherwise authorized to use the Software, and any Licensee
agrees that it obtains no copyright or other intellectual property rights to the Software, except for the license
expressly granted below or in accordance with the terms of their commercial agreement with Licensor (the
“Agreement”). Subject to the terms and conditions of the Agreement, Licensor grants Licensee a non-exclusive,
non-transferable, non-sublicensable, revocable, limited right and license to use the Software, in each case solely
internally within Licensee’s organization for non-commercial purposes and only in connection with the service
provided by Licensor pursuant to the Agreement, and in object code form only. Without Licensor’s express prior
written consent, Licensee may not, directly or indirectly, (i) distribute the Software, any portion thereof, or any
modifications, enhancements, or derivative works of any of the foregoing (collectively, the “Derivatives”) to any
third party, (ii) license, market, sell, offer for sale or otherwise attempt to commercialize any Software, Derivatives,
or portions thereof, (iii) use the Software, Derivatives, or any portion thereof for the benefit of any third party, (iv)
use the Software, Derivatives, or any portion thereof in any manner or with respect to any commercial activity
which competes, or is reasonably likely to compete, with any business that Licensor conducts, proposes to conduct
or demonstrably anticipates conducting, at any time; or (v) seek any patent or other intellectual property rights or
protections over or in connection with any Software of Derivatives.
*/

/*
Sample Terraform config file to create a GCS bucket, role, service account,and key for the cloud with customer-hosted
object storage deployment model on GCP. Additional details and options can be found here: https://docs.getmontecarlo.com/docs/deployment-and-connecting

Note that this will persist a key in the remote state used by Terraform. Please take appropriate measures to protect your remote state.

Usage example (requires Terraform and the gCloud CLI):
  terraform init
  terraform apply

Inputs:
  project_id - The GCP project ID to deploy into [REQUIRED].
  location - The GCP location (region) to deploy into.

Outputs:
  bucket_name - The generated GCS bucket.
  key - The Key file for Monte Carlo to access the bucket.
            Can retrieve via `terraform output -json 'key' | jq -r '.' | base64 -d > key.json`

  These can be used when registering: https://clidocs.getmontecarlo.com/#montecarlo-agents-register-gcs-store
*/


terraform {
  required_version = ">= 1.3"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.1.0"
    }
  }
}

provider "google" {
  project = var.project_id
}

variable "project_id" {
  description = "The GCP project ID to deploy into."
  type        = string
}

variable "location" {
  description = "The GCP location (region) to deploy into."
  type        = string
  default     = "us-east4" # Northern Virginia
}

output "bucket_name" {
  value       = google_storage_bucket.this.name
  description = "The generated GCS bucket."
}

output "key" {
  value       = google_service_account_key.this.private_key
  description = "The Key file for Monte Carlo to access the bucket."
  sensitive   = true
}

resource "random_id" "mcd_id" {
  byte_length = 4
}

resource "google_storage_bucket" "this" {
  name     = "mcd-store-${random_id.mcd_id.hex}"
  location = var.location
  project  = var.project_id
  lifecycle_rule {
    condition {
      age = 90
      matches_prefix = [
        "custom-sql-output-samples/",
        "rca",
        "idempotent",
      ]
    }
    action {
      type = "Delete"
    }
  }
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
}

resource "google_project_iam_custom_role" "this" {
  role_id = "mcdStoreRole${random_id.mcd_id.hex}"
  title   = "MCD Store Role"
  permissions = [
    "storage.objects.create",
    "storage.objects.delete",
    "storage.objects.get",
    "storage.objects.list",
    "storage.objects.update",
    "storage.buckets.get",
    "storage.buckets.getIamPolicy"
  ]
  project = var.project_id
}

resource "google_service_account" "this" {
  account_id   = "mcd-invoker-sa-${random_id.mcd_id.hex}"
  display_name = "MCD Invoker SA"
  project      = var.project_id
}

resource "google_storage_bucket_iam_binding" "mcd_agent_service_sa_binding" {
  bucket = google_storage_bucket.this.name
  role   = "projects/${var.project_id}/roles/${google_project_iam_custom_role.this.role_id}"

  members = [
    "serviceAccount:${google_service_account.this.email}",
  ]
}

resource "google_service_account_key" "this" {
  service_account_id = google_service_account.this.name
  public_key_type    = "TYPE_X509_PEM_FILE"
}