# Monte Carlo Agent with OpenTelemetry Collector - Terraform Module

This Terraform module deploys a Monte Carlo Agent, Data Store, and OpenTelemetry Collector.

## Overview

This module creates:
- A Monte Carlo Agent using the official Terraform module (includes S3 storage bucket)
- An OpenTelemetry Collector using the `monte-carlo-data/otel-collector/aws` module
- All necessary IAM roles and security groups

## Prerequisites

- Terraform >= 1.9.0
- AWS CLI configured with appropriate permissions
- Existing VPC with at least 2 private subnets

## Usage

1. Copy the example variables file:
   ```bash
   cp terraform.tfvars.example terraform.tfvars
   ```

2. Edit `terraform.tfvars` with your specific values:
   - `deployment_name`: Unique name for your deployment
   - `existing_vpc_id`: Your VPC ID
   - `existing_subnet_ids`: List of at least 2 private subnet IDs
   - `cloud_account_id`: Your Monte Carlo cloud account ID

3. Initialize and apply:
   ```bash
   terraform init
   terraform plan
   terraform apply
   ```

## Required Variables

- `deployment_name`: Name for the deployment
- `existing_vpc_id`: VPC ID where resources will be deployed
- `existing_subnet_ids`: List of private subnet IDs (minimum 2)
- `cloud_account_id`: Monte Carlo cloud account ID

## Providers

| Name | Version |
|------|---------|
| aws | ~> 5.0 |

## Modules

| Name | Source | Version |
|------|--------|---------|
| agent | monte-carlo-data/mcd-agent/aws | 1.0.1 |
| opentelemetry_collector | monte-carlo-data/otel-collector/aws | 0.2.0 |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| deployment_name | Name of the deployment (used as prefix for naming resources) | `string` | `"mcd-agent-with-otel"` | no |
| existing_vpc_id | The VPC ID where the Agent and OpenTelemetry Collector will be deployed | `string` | n/a | yes |
| existing_subnet_ids | List of subnet IDs where the Agent and OpenTelemetry Collector will be deployed (should be private subnets) | `list(string)` | n/a | yes |
| existing_security_group_id | Optional additional security group ID to attach to the OpenTelemetry Collector resources | `string` | `"N/A"` | no |
| cloud_account_id | Monte Carlo cloud account ID | `string` | `"590183797493"` | no |
| region | AWS region where resources will be deployed | `string` | `"us-east-1"` | no |
| remote_upgradable | Whether the deployment is remote upgradable | `bool` | `true` | no |
| agent_image_uri | URI of the Agent container image | `string` | `"590183797493.dkr.ecr.*.amazonaws.com/mcd-agent:latest"` | no |
| opentelemetry_collector_external_id | External ID for OpenTelemetry Collector S3 access | `string` | `"N/A"` | no |
| opentelemetry_collector_external_principal_type | Type of principal for external access role | `string` | `"AWS"` | no |
| opentelemetry_collector_external_access_principal | AWS Principal allowed to assume the external access role | `string` | `"N/A"` | no |
| opentelemetry_collector_external_notification_channel_arn | SQS Queue ARN to receive S3 event notifications | `string` | `"N/A"` | no |
| opentelemetry_collector_image | The image URI for the OpenTelemetry Collector container image | `string` | `"otel/opentelemetry-collector-contrib:latest"` | no |
| opentelemetry_collector_existing_bucket_arn | ARN of an existing S3 bucket to store OpenTelemetry data | `string` | `"N/A"` | no |
| external_access_role_name | Custom name of the external access role | `string` | `"N/A"` | no |

## Outputs

| Name | Description |
|------|-------------|
| storage_bucket_name | Name of the S3 bucket used by the Agent and OpenTelemetry Collector |
| storage_bucket_arn | ARN of the S3 bucket used by the Agent and OpenTelemetry Collector |
| agent_function_arn | Agent Function ARN. To be used in registering |
| agent_invocation_role_arn | Assumable role ARN. To be used in registering |
| agent_invocation_role_external_id | Assumable role External ID. To be used in registering |
| opentelemetry_collector_grpc_endpoint | The gRPC endpoint for the OpenTelemetry Collector |
| opentelemetry_collector_http_endpoint | The HTTP endpoint for the OpenTelemetry Collector |
| opentelemetry_collector_external_access_role_arn | The ARN of the IAM role for external access to the OpenTelemetry S3 bucket |
| opentelemetry_collector_security_group_id | The ID of the security group for the OpenTelemetry Collector |

## S3 Bucket

The module uses the S3 bucket created by the Monte Carlo Agent module, which includes:
- Server-side encryption with AES256
- Public access blocked
- SSL/TLS enforcement policy
- Lifecycle policies for data management

### OpenTelemetry Collector Data Management
- **Lifecycle Policy**: Data in `mcd/otel-collector/` prefix expires after 30 days
- **S3 Notifications**: Configurable SQS notifications for OpenTelemetry collector data (optional)

## Security Features

- Server-side encryption with AES256
- Public access blocked
- SSL/TLS enforcement policy
- Configurable security groups
- IAM roles with least privilege access

## Post-Deployment Configuration

After deployment, update the external access configuration:
1. Set `external_id` to a secure random value
2. Set `external_access_principal` to the appropriate AWS account or federated identity
3. Run `terraform apply` again to update the external access role

## Notes
- The OpenTelemetry Collector will use the S3 bucket created by the Monte Carlo Agent module 
unless `opentelemetry_collector_existing_bucket_arn` is specified
- S3 bucket management (encryption, basic lifecycle policies) is handled by the Monte Carlo 
Agent module
- Additional lifecycle policies and S3 notifications for OpenTelemetry collector data are 
configured by this module
- The external access role name defaults to `mcd-otel-collector-EAR` if not specified

## License

Copyright 2023 Monte Carlo Data, Inc.
