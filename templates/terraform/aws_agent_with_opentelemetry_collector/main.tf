# Monte Carlo's Agent with OpenTelemetry Collector - Terraform Configuration
# Copyright 2023 Monte Carlo Data, Inc.

terraform {
  required_version = ">= 1.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

# Data sources
data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# Local values for conditions (equivalent to CloudFormation Conditions)
locals {
  use_existing_telemetry_data_bucket = var.opentelemetry_collector_existing_bucket_arn != "N/A"
  has_notification_channel           = var.opentelemetry_collector_external_notification_channel_arn != "N/A"

  # Extract S3 bucket name from ARN
  storage_bucket_name = split(":", module.agent.mcd_agent_storage_bucket_arn)[5]

  # Common tags
  common_tags = {
    Service  = var.deployment_name
    Provider = "monte-carlo"
  }
}

# Monte Carlo Agent Module
module "agent" {
  source  = "monte-carlo-data/mcd-agent/aws"
  version = "1.0.1"

  cloud_account_id  = var.cloud_account_id
  private_subnets   = var.existing_subnet_ids
  image             = var.agent_image_uri
  region            = var.region
  remote_upgradable = var.remote_upgradable
}

# OpenTelemetry Collector Module
module "opentelemetry_collector" {
  source = "../aws_otel_collector"

  deployment_name                = var.deployment_name
  existing_vpc_id                = var.existing_vpc_id
  existing_subnet_ids            = var.existing_subnet_ids
  telemetry_data_bucket_arn      = local.use_existing_telemetry_data_bucket ? var.opentelemetry_collector_existing_bucket_arn : module.agent.mcd_agent_storage_bucket_arn
  existing_security_group_id     = var.existing_security_group_id
  external_id                    = var.opentelemetry_collector_external_id
  external_access_principal      = var.opentelemetry_collector_external_access_principal
  external_access_principal_type = var.opentelemetry_collector_external_principal_type
  container_image                = var.opentelemetry_collector_image
  external_access_role_name      = var.external_access_role_name
}

# S3 Bucket Lifecycle Configuration for OpenTelemetry Collector data (conditional)
resource "aws_s3_bucket_lifecycle_configuration" "otel_collector_lifecycle" {
  count  = local.use_existing_telemetry_data_bucket ? 0 : 1
  bucket = local.storage_bucket_name

  rule {
    id     = "${var.deployment_name}-otel-collector-expiration"
    status = "Enabled"

    filter {
      prefix = "mcd/otel-collector/"
    }

    expiration {
      days = 30
    }
  }
}

# S3 Bucket Notification Configuration for OpenTelemetry Collector (conditional)
resource "aws_s3_bucket_notification" "storage_notification" {
  count  = (local.has_notification_channel && !local.use_existing_telemetry_data_bucket) ? 1 : 0
  bucket = local.storage_bucket_name

  queue {
    id        = "${var.deployment_name}-opentelemetry-collector-notifications"
    queue_arn = var.opentelemetry_collector_external_notification_channel_arn
    events    = ["s3:ObjectCreated:*", "s3:ObjectRemoved:*"]

    filter_prefix = "mcd/otel-collector/"
  }
}
