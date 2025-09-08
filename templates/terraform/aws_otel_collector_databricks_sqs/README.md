# Monte Carlo SQS Queue for Databricks Integration with OpenTelemetry Collector

This Terraform module creates an SQS queue and associated IAM policies for Monte Carlo's Databricks integration with OpenTelemetry Collector.

## Overview

This module provisions:
- An SQS queue for S3 event notifications
- IAM policies for S3 and Databricks access to the queue
- A managed IAM policy for OpenTelemetry Collector SQS access

## Usage

```hcl
module "aws_otel_collector_databricks_sqs" {
  source = "./aws_otel_collector_databricks_sqs"

  storage_bucket_arn                                    = "arn:aws:s3:::my-telemetry-bucket"
  opentelemetry_collector_external_access_role_arn     = "arn:aws:iam::123456789012:role/my-external-access-role"
}
```

## Variables

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| storage_bucket_arn | ARN of the S3 bucket used by the Agent and OpenTelemetry Collector to store data sampling records and telemetry data | `string` | n/a | yes |
| opentelemetry_collector_external_access_role_arn | The ARN of the IAM role for external access to the OpenTelemetry S3 bucket | `string` | n/a | yes |

## Outputs

| Name | Description |
|------|-------------|
| sqs_notification_queue_arn | The ARN of the SQS queue for S3 event notifications |
| sqs_notification_queue_url | The URL of the SQS queue for S3 event notifications |
| opentelemetry_collector_sqs_access_policy_arn | The ARN of the IAM managed policy for SQS access |

## Resources Created

- `aws_sqs_queue.notification_queue` - SQS queue for notifications
- `aws_sqs_queue_policy.s3_access_policy` - Policy allowing S3 to send messages to the queue
- `aws_sqs_queue_policy.databricks_access_policy` - Policy allowing Databricks to access the queue
- `aws_iam_policy.opentelemetry_collector_sqs_access_policy` - Managed policy for OpenTelemetry Collector SQS access
- `aws_iam_role_policy_attachment.opentelemetry_collector_sqs_access_policy_attachment` - Attaches the policy to the external access role

## Requirements

| Name | Version |
|------|---------|
| terraform | >= 1.0 |
| aws | ~> 5.0 |

## License

Copyright 2023 Monte Carlo Data, Inc.
