# Monte Carlo's Agent with OpenTelemetry Collector - Terraform Configuration for GCP
# Copyright 2023 Monte Carlo Data, Inc.

terraform {
  required_version = ">= 1.3.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0, < 7.0"
    }
  }
}

# Data sources
data "google_project" "current" {
  project_id = var.project_id
}

# Local values
locals {
  # Determine VPC connector to use
  create_shared_vpc_connector = var.existing_vpc_connector == null
  vpc_connector_id            = local.create_shared_vpc_connector ? google_vpc_access_connector.shared_connector[0].id : var.existing_vpc_connector

  # Common labels
  common_labels = {
    managed_by = "terraform"
    service    = var.deployment_name
    provider   = "monte-carlo"
  }
}

# Shared VPC Access Connector (created if existing_vpc_connector is not provided)
resource "google_vpc_access_connector" "shared_connector" {
  count = local.create_shared_vpc_connector ? 1 : 0

  project       = var.project_id
  name          = "${var.deployment_name}-vpc"
  region        = var.region
  network       = var.vpc_network
  ip_cidr_range = var.vpc_subnet

  min_instances = 2
  max_instances = 3
}

# Monte Carlo Agent Module
# Note: The agent module uses the shared VPC connector via local.vpc_connector_id,
# which creates an implicit dependency on google_vpc_access_connector.shared_connector
module "agent" {
  source  = "monte-carlo-data/mcd-agent/google"
  version = "1.1.0"

  project_id   = var.project_id
  location     = var.region
  generate_key = var.generate_key

  # VPC configuration - use the shared connector
  # The reference to local.vpc_connector_id creates an implicit dependency
  vpc_access = {
    connector = local.vpc_connector_id
    egress    = "PRIVATE_RANGES_ONLY"
  }
}

# OpenTelemetry Collector Module
module "opentelemetry_collector" {
  source  = "monte-carlo-data/otel-collector/google"
  version = "0.0.2"

  project_id      = var.project_id
  deployment_name = var.deployment_name
  region          = var.region
  vpc_network     = var.vpc_network

  # Use the shared VPC connector
  existing_vpc_connector = local.vpc_connector_id

  # Container configuration
  container_image = var.opentelemetry_collector_image
  grpc_port       = var.opentelemetry_collector_grpc_port
  http_port       = var.opentelemetry_collector_http_port

  # Cloud Run configuration
  min_instances   = var.opentelemetry_collector_min_instances
  max_instances   = var.opentelemetry_collector_max_instances
  cpu             = var.opentelemetry_collector_cpu
  memory          = var.opentelemetry_collector_memory
  timeout_seconds = var.opentelemetry_collector_timeout_seconds
  concurrency     = var.opentelemetry_collector_concurrency

  # OTEL configuration
  batch_timeout          = var.opentelemetry_collector_batch_timeout
  batch_size             = var.opentelemetry_collector_batch_size
  memory_limit_mib       = var.opentelemetry_collector_memory_limit_mib
  memory_spike_limit_mib = var.opentelemetry_collector_memory_spike_limit_mib

  # BigQuery integration (optional)
  bigquery_table_id = var.opentelemetry_collector_bigquery_table_id

  # Labels and protection
  labels              = local.common_labels
  deletion_protection = var.deletion_protection

  depends_on = [google_vpc_access_connector.shared_connector]
}

