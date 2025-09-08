# Variables for Monte Carlo SQS queue for Databricks integration with OpenTelemetry Collector

variable "storage_bucket_arn" {
  description = "ARN of the S3 bucket used by the Agent and OpenTelemetry Collector to store data sampling records and telemetry data."
  type        = string
  validation {
    condition     = can(regex("^arn:aws:s3:::.*$", var.storage_bucket_arn))
    error_message = "Must be a valid S3 bucket ARN"
  }
}

variable "opentelemetry_collector_external_access_role_name" {
  description = "The name of the IAM role for external access to the OpenTelemetry S3 bucket"
  type        = string
  validation {
    condition     = can(regex("^[a-zA-Z0-9+=,.@_-]+$", var.opentelemetry_collector_external_access_role_name))
    error_message = "Must be a valid IAM role name"
  }
}
