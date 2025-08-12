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
Sample Terraform config file to create a S3 bucket and assumable IAM role for the cloud with customer-hosted
object storage deployment model on AWS. Additional details and options can be found here: https://docs.getmontecarlo.com/docs/deployment-and-connecting

Notice: Do not apply this Terraform configuration from this directory as it contains other configurations that may create unintended resources. Either:
1. Copy this file to a new directory and apply from there
2. Use the -target parameter to specifically apply only these resources
3. Create a module from this configuration and reference it from a separate root configuration

Usage example (requires Terraform and the AWS CLI):
  AWS_DEFAULT_PROFILE=<Your AWS CLI Profile> terraform init
  AWS_DEFAULT_PROFILE=<Your AWS CLI Profile> terraform apply

Inputs:
  monte_carlo_cloud_account_id - The Monte Carlo AWS Account ID (default: 590183797493)
  data_store_region - AWS region to deploy resources (default: "us-east-1")

Outputs:
  object_store_bucket_name - The generated S3 bucket name
  object_store_role_arn - The IAM role ARN
  object_store_role_external_id - The external ID for the role

These can be used when registering: https://docs.getmontecarlo.com/docs/direct-connection-with-an-aws-data-store
*/

terraform {
  required_version = ">= 1.3"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

provider "aws" {
  region = var.data_store_region
  default_tags {
    tags = {
      "MonteCarloData" = ""
    }
  }
}

variable "monte_carlo_cloud_account_id" {
  description = <<EOF
For deployments on the V2 Platform, use 590183797493. Accounts created after April 24th, 2024, 
will automatically be on the V2 platform or newer. If you are using an older version of the platform, 
please contact your Monte Carlo representative for the ID.
EOF
  type        = string
  default     = "590183797493"
}

variable "data_store_region" {
  description = "AWS region to deploy resources"
  type        = string
  default     = "us-east-1"
}

data "aws_caller_identity" "current" {}

locals {
  should_skip_cloud_account_policy = var.monte_carlo_cloud_account_id == "590183797493"
  external_id                      = random_id.this.hex
}

resource "random_id" "this" {
  byte_length = 4
}

resource "aws_s3_bucket" "this" {
  bucket_prefix = "monte-carlo-object-store-"
}

resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  bucket = aws_s3_bucket.this.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "this" {
  bucket = aws_s3_bucket.this.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "this" {
  bucket = aws_s3_bucket.this.id

  rule {
    id     = "custom-sql-output-samples-expiration"
    status = "Enabled"

    expiration {
      days = 90
    }

    filter {
      prefix = "custom-sql-output-samples/"
    }
  }

  rule {
    id     = "rca-expiration"
    status = "Enabled"

    expiration {
      days = 90
    }

    filter {
      prefix = "rca"
    }
  }

  rule {
    id     = "idempotent-expiration"
    status = "Enabled"

    expiration {
      days = 90
    }

    filter {
      prefix = "idempotent/"
    }
  }
}

resource "aws_iam_role" "this" {
  name_prefix = "monte-carlo-object-store-role-"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          AWS = local.should_skip_cloud_account_policy ? [
            "arn:aws:iam::590183797493:root"
            ] : [
            "arn:aws:iam::${var.monte_carlo_cloud_account_id}:root",
            "arn:aws:iam::590183797493:root"
          ]
        }
        Action = "sts:AssumeRole"
        Condition = {
          StringEquals = {
            "sts:ExternalId" = local.external_id
          }
        }
      }
    ]
  })
}

# IAM Role Policy
resource "aws_iam_role_policy" "this" {
  name = "s3-policy"
  role = aws_iam_role.this.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetObject",
          "s3:DeleteObject",
          "s3:ListBucket",
          "s3:GetBucketPublicAccessBlock",
          "s3:GetBucketPolicyStatus",
          "s3:GetBucketAcl"
        ]
        Resource = [
          aws_s3_bucket.this.arn,
          "${aws_s3_bucket.this.arn}/*"
        ]
      }
    ]
  })
}

# Outputs
output "object_store_bucket_name" {
  description = "Name of the S3 bucket. To be used in registering."
  value       = aws_s3_bucket.this.id
}


output "object_store_role_arn" {
  description = "ARN for the assumable role. To be used in registering."
  value       = aws_iam_role.this.arn
}

output "object_store_role_external_id" {
  description = "External ID for the assumable role. To be used in registering."
  value       = local.external_id
}