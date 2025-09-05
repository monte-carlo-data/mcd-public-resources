# Monte Carlo's OpenTelemetry Collector Service - Terraform

This Terraform configuration deploys Monte Carlo's OpenTelemetry Collector Service on AWS ECS.

## Architecture

The configuration creates:
- ECS Fargate cluster and service
- Network Load Balancer with gRPC and HTTP listeners
- Security groups and IAM roles
- CloudWatch log group
- External access role for S3 bucket access

## Prerequisites

- Terraform >= 1.0
- AWS CLI configured with appropriate permissions
- Existing VPC with at least 2 private subnets
- S3 bucket for storing telemetry data

## Usage

1. Copy the example variables file:
   ```bash
   cp terraform.tfvars.example terraform.tfvars
   ```

2. Edit `terraform.tfvars` with your specific values:
   - `deployment_name`: Unique name for your deployment
   - `existing_vpc_id`: Your VPC ID
   - `existing_subnet_ids`: List of at least 2 private subnet IDs
   - `telemetry_data_bucket_arn`: ARN of your S3 bucket

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
- `telemetry_data_bucket_arn`: S3 bucket ARN for telemetry data

## Optional Variables

All other variables have sensible defaults but can be customized:
- Network ports (gRPC: 4317, HTTP: 4318)
- ECS task configuration (CPU, memory, desired count)
- OpenTelemetry configuration (batch settings, memory limits)
- External access configuration for S3 bucket

## Outputs

- `opentelemetry_collector_grpc_endpoint`: gRPC endpoint URL
- `opentelemetry_collector_http_endpoint`: HTTP endpoint URL  
- `opentelemetry_collector_external_access_role_arn`: IAM role ARN for external access
- `opentelemetry_collector_security_group_id`: Security group ID

## Post-Deployment Configuration

After deployment, update the external access configuration:
1. Set `external_id` to a secure random value
2. Set `external_access_principal` to the appropriate AWS account or federated identity
3. Run `terraform apply` again to update the external access role

## License

Copyright 2023 Monte Carlo Data, Inc.
