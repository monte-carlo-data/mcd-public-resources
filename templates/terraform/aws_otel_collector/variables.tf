# Variables for Monte Carlo's OpenTelemetry Collector Service

variable "deployment_name" {
  description = "Name of the deployment (used for naming resources)"
  type        = string
}

variable "existing_vpc_id" {
  description = "VPC ID to deploy the OpenTelemetry Collector into."
  type        = string
  validation {
    condition     = can(regex("^(vpc[e]?-[0-9a-f]*)$", var.existing_vpc_id))
    error_message = "VPC ID must match pattern ^(vpc[e]?-[0-9a-f]*)$"
  }
}

variable "existing_subnet_ids" {
  description = "List of private subnet IDs (at least 2) for deploying the OpenTelemetry Collector."
  type        = list(string)
  validation {
    condition     = length(var.existing_subnet_ids) >= 2
    error_message = "At least 2 subnet IDs must be provided."
  }
}

variable "telemetry_data_bucket_arn" {
  description = "ARN of the S3 bucket to store OpenTelemetry data such as traces, metrics, and logs."
  type        = string
}

variable "existing_security_group_id" {
  description = "Optional additional security group ID to attach to the OpenTelemetry Collector resources."
  type        = string
  default     = "N/A"
  validation {
    condition     = can(regex("^(|N/A|sg-[0-9a-f]*)$", var.existing_security_group_id))
    error_message = "Must be either empty, N/A, or a valid security group ID (sg-xxxxxxxxx)"
  }
}

variable "grpc_port" {
  description = "Port for OTLP gRPC receiver"
  type        = number
  default     = 4317
  validation {
    condition     = var.grpc_port >= 1024 && var.grpc_port <= 65535
    error_message = "Port must be between 1024 and 65535."
  }
}

variable "http_port" {
  description = "Port for OTLP HTTP receiver"
  type        = number
  default     = 4318
  validation {
    condition     = var.http_port >= 1024 && var.http_port <= 65535
    error_message = "Port must be between 1024 and 65535."
  }
}

# Validation to ensure ports are different (equivalent to CloudFormation Rules)
check "port_validation" {
  assert {
    condition     = var.grpc_port != var.http_port
    error_message = "GRPCPort and HTTPPort must be different values to avoid conflicts"
  }
}

variable "task_desired_count" {
  description = "Desired number of running tasks for the OpenTelemetry Collector service"
  type        = number
  default     = 2
  validation {
    condition     = var.task_desired_count >= 1 && var.task_desired_count <= 10
    error_message = "Task desired count must be between 1 and 10."
  }
}

variable "task_cpu" {
  description = "CPU units for the task (1024 = 1 vCPU)"
  type        = number
  default     = 1024
  validation {
    condition     = contains([256, 512, 1024, 2048, 4096], var.task_cpu)
    error_message = "Task CPU must be one of: 256, 512, 1024, 2048, 4096."
  }
}

variable "task_memory" {
  description = "Memory for the task in MB"
  type        = number
  default     = 2048
  validation {
    condition     = contains([512, 1024, 2048, 3072, 4096, 5120, 6144, 7168, 8192], var.task_memory)
    error_message = "Task memory must be one of: 512, 1024, 2048, 3072, 4096, 5120, 6144, 7168, 8192."
  }
}

variable "container_image" {
  description = "OpenTelemetry Collector container image"
  type        = string
  default     = "otel/opentelemetry-collector-contrib:latest"
}

variable "batch_timeout" {
  description = "Timeout for batch processor in seconds"
  type        = string
  default     = "10s"
}

variable "batch_size" {
  description = "Batch size for sending telemetry data"
  type        = number
  default     = 1024
  validation {
    condition     = var.batch_size >= 100 && var.batch_size <= 10000
    error_message = "Batch size must be between 100 and 10000."
  }
}

variable "memory_limit_mib" {
  description = "Memory limit for the collector in MiB"
  type        = number
  default     = 1500
  validation {
    condition     = var.memory_limit_mib >= 512 && var.memory_limit_mib <= 4096
    error_message = "Memory limit must be between 512 and 4096 MiB."
  }
}

variable "memory_spike_limit_mib" {
  description = "Memory spike limit for the collector in MiB"
  type        = number
  default     = 512
  validation {
    condition     = var.memory_spike_limit_mib >= 256 && var.memory_spike_limit_mib <= 2048
    error_message = "Memory spike limit must be between 256 and 2048 MiB."
  }
}

variable "external_id" {
  description = "External ID to access the S3 bucket. Update this value later after the stack is created."
  type        = string
  default     = "N/A"
}

variable "external_access_principal" {
  description = "Principal (AWS ARN/account ID or Federated identifier) allowed to assume the external access role."
  type        = string
  default     = "N/A"
}

variable "external_access_principal_type" {
  description = "Type of principal for external access role"
  type        = string
  default     = "AWS"
  validation {
    condition     = contains(["AWS", "Federated"], var.external_access_principal_type)
    error_message = "External access principal type must be either 'AWS' or 'Federated'."
  }
}

variable "external_access_role_name" {
  description = "Custom name of the external access role. If left empty, will use the default name."
  type        = string
  default     = "N/A"
}
