# Outputs for Monte Carlo SQS queue for Databricks integration with OpenTelemetry Collector

output "sqs_notification_queue_arn" {
  description = "The ARN of the SQS queue for S3 event notifications"
  value       = aws_sqs_queue.notification_queue.arn
}

output "sqs_notification_queue_url" {
  description = "The URL of the SQS queue for S3 event notifications"
  value       = aws_sqs_queue.notification_queue.url
}

output "opentelemetry_collector_sqs_access_policy_arn" {
  description = "The ARN of the IAM managed policy for SQS access"
  value       = aws_iam_policy.opentelemetry_collector_sqs_access_policy.arn
}
