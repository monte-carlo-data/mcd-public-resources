# Variables for Monte Carlo's Agent with OpenTelemetry Collector

variable "deployment_name" {
  description = "Name of the deployment (used as prefix for naming resources)"
  type        = string
  default     = "mcd-agent-with-otel"
  validation {
    condition     = can(regex("^[a-zA-Z0-9-]+$", var.deployment_name))
    error_message = "Deployment name must contain only alphanumeric characters and hyphens."
  }
}

variable "existing_vpc_id" {
  description = "The VPC ID where the Agent and OpenTelemetry Collector will be deployed"
  type        = string
  validation {
    condition     = can(regex("^(vpc[e]?-[0-9a-f]*)$", var.existing_vpc_id))
    error_message = "VPC ID must match pattern ^(vpc[e]?-[0-9a-f]*)$"
  }
}

variable "existing_subnet_ids" {
  description = "List of subnet IDs where the Agent and OpenTelemetry Collector will be deployed (should be private subnets)"
  type        = list(string)
  validation {
    condition     = length(var.existing_subnet_ids) >= 2
    error_message = "At least 2 subnet IDs must be provided."
  }
}

variable "existing_security_group_id" {
  description = "Optional additional security group ID to attach to the OpenTelemetry Collector resources. If provided, this security group will be added to the Network Load Balancer and ECS Service."
  type        = string
  default     = "N/A"
  validation {
    condition     = can(regex("^(|N/A|sg-[0-9a-f]*)$", var.existing_security_group_id))
    error_message = "Must be either empty, N/A, or a valid security group ID (sg-xxxxxxxxx)"
  }
}

variable "cloud_account_id" {
  description = "For deployments on the V2 Platform, use the Collection AWS account ID in the Account information page. Accounts created after April 24th, 2024, will automatically be on the V2 platform or newer. If you are using an older version of the platform, please contact your Monte Carlo representative for the ID."
  type        = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.cloud_account_id))
    error_message = "Must be a valid 12-digit AWS account ID"
  }
}

variable "region" {
  description = "AWS region where resources will be deployed"
  type        = string
  default     = "us-east-1"
}

variable "remote_upgradable" {
  description = "Whether the deployment is remote upgradable"
  type        = bool
  default     = true
}

variable "agent_image_uri" {
  description = "URI of the Agent container image (ECR Repo). Note that the region automatically maps to where this stack is deployed in."
  type        = string
  default     = "590183797493.dkr.ecr.*.amazonaws.com/mcd-agent:latest"
}

variable "opentelemetry_collector_external_id" {
  description = "External ID for OpenTelemetry Collector S3 access. Update this value later after the Terraform deployment is created to the value provided by your warehouse (e.g. Snowflake, Databricks, BigQuery)"
  type        = string
  default     = "N/A"
}

variable "opentelemetry_collector_external_principal_type" {
  description = "Type of principal for OpenTelemetry Collector external access role. For Snowflake and Databricks, use 'AWS'. For BigQuery, use 'Federated'."
  type        = string
  default     = "AWS"
  validation {
    condition     = contains(["AWS", "Federated"], var.opentelemetry_collector_external_principal_type)
    error_message = "External principal type must be either 'AWS' or 'Federated'."
  }
}

variable "opentelemetry_collector_external_access_principal" {
  description = "AWS Principal (ARN or account ID) allowed to assume the OpenTelemetry Collector external access role. If left empty, will use the current AWS account ID. Update this value later after the Terraform deployment is created to the value provided by your warehouse (e.g. Snowflake, Databricks, BigQuery)"
  type        = string
  default     = "N/A"
  validation {
    condition     = can(regex("^(|N/A|[0-9]{12}|arn:aws:.*:.*)$", var.opentelemetry_collector_external_access_principal))
    error_message = "Must be either empty, N/A, a 12-digit AWS account ID, or a valid AWS ARN"
  }
}

variable "opentelemetry_collector_external_notification_channel_arn" {
  description = "SQS Queue ARN or SNS Topic ARN to receive S3 event notifications for telemetry data. If left empty, no notifications will be configured. Update this value later after the Terraform deployment is created to the value provided by your warehouse (e.g. Snowflake, Databricks, BigQuery)."
  type        = string
  default     = "N/A"
  validation {
    condition     = can(regex("^(|N/A|arn:aws:(sqs|sns):[^:]+:[0-9]{12}:[^:]+)$", var.opentelemetry_collector_external_notification_channel_arn))
    error_message = "Must be either empty, N/A, a valid SQS ARN (arn:aws:sqs:region:account:queue-name), or a valid SNS ARN (arn:aws:sns:region:account:topic-name)"
  }
}

variable "opentelemetry_collector_image" {
  description = "The image URI for the OpenTelemetry Collector container image."
  type        = string
  default     = "otel/opentelemetry-collector-contrib:latest"
}

variable "opentelemetry_collector_existing_bucket_arn" {
  description = "ARN of an existing S3 bucket to store OpenTelemetry data. If left empty, the data store bucket will be used."
  type        = string
  default     = "N/A"
}

variable "external_access_role_name" {
  description = "Custom name of the external access role. If left empty, will use the default name of 'mcd-otel-collector-EAR'."
  type        = string
  default     = "N/A"
}

variable "deploy_redshift_resources" {
  description = "Whether to deploy Redshift-specific resources. When true, creates a bucket policy Redshift notification configuration access and deploys the Otel Collector's Lambda UDF."
  type        = bool
  default     = false
}
