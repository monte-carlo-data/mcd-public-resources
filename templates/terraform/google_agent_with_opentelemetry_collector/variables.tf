# Variables for Monte Carlo's Agent with OpenTelemetry Collector on GCP

# =============================================================================
# Required Variables
# =============================================================================

variable "project_id" {
  description = "GCP project ID where resources will be created"
  type        = string

  validation {
    condition     = length(var.project_id) > 0
    error_message = "Project ID must not be empty."
  }
}

variable "deployment_name" {
  description = "Name prefix for all resources created by this module (max 20 chars due to VPC connector name limits)"
  type        = string
  default     = "mcd-agent-with-otel"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,18}[a-z0-9]$", var.deployment_name))
    error_message = "Deployment name must be 1-20 chars: lowercase, numbers, hyphens, start with letter."
  }
}

variable "region" {
  description = "GCP region where resources will be deployed"
  type        = string

  validation {
    condition     = length(var.region) > 0
    error_message = "Region must not be empty."
  }
}

variable "vpc_network" {
  description = "VPC network name or self-link for internal access (e.g., 'projects/my-project/global/networks/my-vpc')"
  type        = string

  validation {
    condition     = length(var.vpc_network) > 0
    error_message = "VPC network must not be empty."
  }
}

# =============================================================================
# VPC Configuration
# =============================================================================

variable "vpc_subnet" {
  description = "Subnet CIDR range for VPC connector (e.g., '10.8.0.0/28'). Required if existing_vpc_connector is not provided."
  type        = string
  default     = null
}

variable "existing_vpc_connector" {
  description = "ID of an existing VPC connector to use. If not provided, a new shared connector will be created for both Agent and OpenTelemetry Collector."
  type        = string
  default     = null
}

# =============================================================================
# Agent Configuration
# =============================================================================

variable "generate_key" {
  description = "Whether to generate a service account key for the Agent. If true, the key will be stored in Terraform state."
  type        = bool
  default     = true
}

# =============================================================================
# OpenTelemetry Collector - Container Configuration
# =============================================================================

variable "opentelemetry_collector_image" {
  description = "Docker image for the OpenTelemetry Collector"
  type        = string
  default     = "otel/opentelemetry-collector-contrib:latest"
}

variable "opentelemetry_collector_grpc_port" {
  description = "Port for OTLP gRPC receiver"
  type        = number
  default     = 4317
}

variable "opentelemetry_collector_http_port" {
  description = "Port for OTLP HTTP receiver"
  type        = number
  default     = 4318
}

# =============================================================================
# OpenTelemetry Collector - Cloud Run Configuration
# =============================================================================

variable "opentelemetry_collector_min_instances" {
  description = "Minimum number of Cloud Run instances (0 for scale-to-zero)"
  type        = number
  default     = 1
}

variable "opentelemetry_collector_max_instances" {
  description = "Maximum number of Cloud Run instances"
  type        = number
  default     = 10
}

variable "opentelemetry_collector_cpu" {
  description = "CPU allocation for Cloud Run container (e.g., '1' for 1 vCPU, '2' for 2 vCPUs)"
  type        = string
  default     = "1"
}

variable "opentelemetry_collector_memory" {
  description = "Memory allocation for Cloud Run container (e.g., '512Mi', '2Gi', '4Gi')"
  type        = string
  default     = "2Gi"
}

variable "opentelemetry_collector_timeout_seconds" {
  description = "Request timeout in seconds (max 3600 for Cloud Run 2nd gen)"
  type        = number
  default     = 300
}

variable "opentelemetry_collector_concurrency" {
  description = "Maximum number of concurrent requests per Cloud Run instance"
  type        = number
  default     = 80
}

# =============================================================================
# OpenTelemetry Collector - OTEL Configuration
# =============================================================================

variable "opentelemetry_collector_batch_timeout" {
  description = "Batch processor timeout (e.g., '10s', '1m')"
  type        = string
  default     = "10s"
}

variable "opentelemetry_collector_batch_size" {
  description = "Batch processor send_batch_size"
  type        = number
  default     = 1024
}

variable "opentelemetry_collector_memory_limit_mib" {
  description = "Memory limiter limit in MiB"
  type        = number
  default     = 1500
}

variable "opentelemetry_collector_memory_spike_limit_mib" {
  description = "Memory limiter spike limit in MiB"
  type        = number
  default     = 512
}

# =============================================================================
# OpenTelemetry Collector - BigQuery Integration
# =============================================================================

variable "opentelemetry_collector_bigquery_table_id" {
  description = "BigQuery table ID for Pub/Sub subscription to write to (format: project.dataset.table). If provided, creates a Pub/Sub subscription with BigQuery integration."
  type        = string
  default     = null
}

# =============================================================================
# Protection Settings
# =============================================================================

variable "deletion_protection" {
  description = "Enable deletion protection for Cloud Run service (recommended for production)"
  type        = bool
  default     = true
}

