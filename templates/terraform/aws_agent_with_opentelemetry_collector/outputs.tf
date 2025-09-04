# Outputs for Monte Carlo's Agent with OpenTelemetry Collector

output "storage_bucket_name" {
  description = "Name of the S3 bucket used by the Agent and OpenTelemetry Collector to store data sampling records and telemetry data."
  value       = local.storage_bucket_name
}

output "storage_bucket_arn" {
  description = "ARN of the S3 bucket used by the Agent and OpenTelemetry Collector to store data sampling records and telemetry data."
  value       = module.agent.mcd_agent_storage_bucket_arn
}

output "agent_function_arn" {
  description = "Agent Function ARN. To be used in registering."
  value       = module.agent.mcd_agent_function_arn
}

output "agent_invocation_role_arn" {
  description = "Assumable role ARN. To be used in registering."
  value       = module.agent.mcd_agent_invoker_role_arn
}

output "agent_invocation_role_external_id" {
  description = "Assumable role External ID. To be used in registering."
  value       = module.agent.mcd_agent_invoker_role_external_id
}

output "opentelemetry_collector_grpc_endpoint" {
  description = "The gRPC endpoint for the OpenTelemetry Collector"
  value       = module.opentelemetry_collector.opentelemetry_collector_grpc_endpoint
}

output "opentelemetry_collector_http_endpoint" {
  description = "The HTTP endpoint for the OpenTelemetry Collector"
  value       = module.opentelemetry_collector.opentelemetry_collector_http_endpoint
}

output "opentelemetry_collector_external_access_role_arn" {
  description = "The ARN of the IAM role for external access to the OpenTelemetry S3 bucket"
  value       = module.opentelemetry_collector.opentelemetry_collector_external_access_role_arn
}

output "opentelemetry_collector_security_group_id" {
  description = "The ID of the security group for the OpenTelemetry Collector"
  value       = module.opentelemetry_collector.opentelemetry_collector_security_group_id
}
