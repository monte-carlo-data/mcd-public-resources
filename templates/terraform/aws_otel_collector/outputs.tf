# Outputs for Monte Carlo's OpenTelemetry Collector Service

output "opentelemetry_collector_grpc_endpoint" {
  description = "The gRPC endpoint for the OpenTelemetry Collector"
  value       = "${aws_lb.network_load_balancer.dns_name}:${var.grpc_port}"
}

output "opentelemetry_collector_http_endpoint" {
  description = "The HTTP endpoint for the OpenTelemetry Collector"
  value       = "http://${aws_lb.network_load_balancer.dns_name}:${var.http_port}"
}

output "opentelemetry_collector_external_access_role_arn" {
  description = "The ARN of the IAM role for external access to the OpenTelemetry S3 bucket"
  value       = aws_iam_role.external_access_role.arn
}

output "opentelemetry_collector_security_group_id" {
  description = "The ID of the security group for the OpenTelemetry Collector"
  value       = aws_security_group.security_group.id
}
