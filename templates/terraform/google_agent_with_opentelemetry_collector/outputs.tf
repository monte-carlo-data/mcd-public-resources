# Outputs for Monte Carlo's Agent with OpenTelemetry Collector on GCP

# =============================================================================
# Agent Outputs
# =============================================================================

output "agent_uri" {
  description = "The URL for the Monte Carlo Agent"
  value       = module.agent.mcd_agent_uri
}

output "agent_invoker_key" {
  description = "The Key file for Monte Carlo to invoke the agent (if generate_key is true)"
  value       = module.agent.mcd_agent_invoker_key
  sensitive   = true
}

output "agent_invoker_sa" {
  description = "The agent invoker service account email"
  value       = module.agent.mcd_agent_invoker_sa
}

output "agent_storage_bucket_name" {
  description = "Name of the GCS bucket used by the Agent for data sampling and troubleshooting"
  value       = module.agent.mcd_agent_storage
}

# =============================================================================
# OpenTelemetry Collector Outputs
# =============================================================================

output "opentelemetry_collector_service_url" {
  description = "Cloud Run service URL for the OpenTelemetry Collector"
  value       = module.opentelemetry_collector.otel_collector_service_url
}

output "opentelemetry_collector_service_name" {
  description = "Name of the OpenTelemetry Collector Cloud Run service"
  value       = module.opentelemetry_collector.otel_collector_service_name
}

output "opentelemetry_collector_grpc_endpoint" {
  description = "The gRPC endpoint for the OpenTelemetry Collector"
  value       = module.opentelemetry_collector.otel_collector_grpc_endpoint
}

output "opentelemetry_collector_http_endpoint" {
  description = "The HTTP endpoint for the OpenTelemetry Collector"
  value       = module.opentelemetry_collector.otel_collector_http_endpoint
}

output "opentelemetry_collector_service_account_email" {
  description = "Email of the service account used by the OpenTelemetry Collector"
  value       = module.opentelemetry_collector.otel_collector_service_account_email
}

# =============================================================================
# Pub/Sub Outputs
# =============================================================================

output "opentelemetry_collector_traces_topic_name" {
  description = "Name of the Pub/Sub topic for traces"
  value       = module.opentelemetry_collector.otel_collector_traces_topic_name
}

output "opentelemetry_collector_traces_topic_id" {
  description = "Full ID of the Pub/Sub topic for traces"
  value       = module.opentelemetry_collector.otel_collector_traces_topic_id
}

output "opentelemetry_collector_traces_subscription_name" {
  description = "Name of the Pub/Sub subscription for traces (if BigQuery integration is configured)"
  value       = module.opentelemetry_collector.otel_collector_traces_subscription_name
}

output "opentelemetry_collector_traces_subscription_id" {
  description = "Full ID of the Pub/Sub subscription for traces (if BigQuery integration is configured)"
  value       = module.opentelemetry_collector.otel_collector_traces_subscription_id
}

# =============================================================================
# VPC Connector Outputs
# =============================================================================

output "vpc_connector_id" {
  description = "ID of the VPC connector used by Agent and OpenTelemetry Collector"
  value       = local.vpc_connector_id
}

output "vpc_connector_name" {
  description = "Name of the VPC connector (if created by this module)"
  value       = local.create_shared_vpc_connector ? google_vpc_access_connector.shared_connector[0].name : null
}

