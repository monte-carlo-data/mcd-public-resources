# Monte Carlo SQS queue for Databricks integration with OpenTelemetry Collector - Terraform Configuration
# Copyright 2023 Monte Carlo Data, Inc.

terraform {
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
data "aws_iam_role" "opentelemetry_collector_external_access_role" {
  name = var.opentelemetry_collector_external_access_role_name
}

# Local values
locals {
  # Common tags
  common_tags = {
    Service  = "mcd-otel-collector"
    Provider = "monte-carlo"
  }
}

# SQS Notification Queue
resource "aws_sqs_queue" "notification_queue" {
  name                       = "mcd-otel-collector-databricks-notifications"
  visibility_timeout_seconds = 30
  message_retention_seconds  = 1209600
  receive_wait_time_seconds  = 20

  tags = local.common_tags
}

# SQS S3 Access Policy
resource "aws_sqs_queue_policy" "s3_access_policy" {
  queue_url = aws_sqs_queue.notification_queue.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "s3.amazonaws.com"
        }
        Action = [
          "sqs:SendMessage"
        ]
        Resource = aws_sqs_queue.notification_queue.arn
        Condition = {
          ArnLike = {
            "aws:SourceArn" = var.storage_bucket_arn
          }
        }
      }
    ]
  })
}

# SQS Databricks Access Policy
resource "aws_sqs_queue_policy" "databricks_access_policy" {
  queue_url = aws_sqs_queue.notification_queue.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          AWS = data.aws_iam_role.opentelemetry_collector_external_access_role.arn
        }
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
          "sqs:GetQueueUrl"
        ]
        Resource = aws_sqs_queue.notification_queue.arn
      }
    ]
  })
}

# OpenTelemetry Collector SQS Access Policy
resource "aws_iam_policy" "opentelemetry_collector_sqs_access_policy" {
  name        = "mcd-otel-collector-sqs-access-policy"
  description = "Policy allowing OpenTelemetry Collector to access SQS queue for Databricks notifications"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
          "sqs:GetQueueUrl"
        ]
        Resource = aws_sqs_queue.notification_queue.arn
      }
    ]
  })

  tags = local.common_tags
}

# Attach the policy to the external access role
resource "aws_iam_role_policy_attachment" "opentelemetry_collector_sqs_access_policy_attachment" {
  role       = var.opentelemetry_collector_external_access_role_name
  policy_arn = aws_iam_policy.opentelemetry_collector_sqs_access_policy.arn
}
