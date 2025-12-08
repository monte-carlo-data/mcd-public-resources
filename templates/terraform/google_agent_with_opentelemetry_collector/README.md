# Monte Carlo Agent with OpenTelemetry Collector - Terraform Module (GCP)

This Terraform module deploys a Monte Carlo Agent, Data Store, and OpenTelemetry Collector on Google Cloud Platform.

## Overview

This module creates:
- A Monte Carlo Agent using the official Terraform module (includes GCS storage bucket)
- An OpenTelemetry Collector using the `monte-carlo-data/otel-collector/google` module
- A shared VPC Access Connector (optional, can use existing)
- Pub/Sub topic for trace data export
- BigQuery integration for storing trace data (optional)

## Prerequisites

- Terraform >= 1.3.0
- Google Cloud CLI configured with appropriate permissions
- Existing VPC network with at least one subnet

## Usage

1. Copy the example variables file:
   ```bash
   cp terraform.tfvars.example terraform.tfvars
   ```

2. Edit `terraform.tfvars` with your specific values:
   - `project_id`: Your GCP project ID
   - `deployment_name`: Unique name for your deployment
   - `region`: GCP region for deployment
   - `vpc_network`: Your VPC network name or self-link
   - `vpc_subnet`: CIDR range for VPC connector (or provide `existing_vpc_connector`)

3. Initialize and apply:
   ```bash
   terraform init
   terraform plan
   terraform apply
   ```

4. Register the Agent with Monte Carlo using the outputs:
   ```bash
   montecarlo agents register-gcp-agent \
     --url $(terraform output -raw agent_uri) \
     --key-file <(terraform output -json 'agent_invoker_key' | jq -r '.[0]' | base64 -d)
   ```

## Providers

| Name | Version |
|------|---------|
| google | >= 5.0, < 7.0 |

## Modules

| Name | Source | Version |
|------|--------|---------|
| agent | monte-carlo-data/mcd-agent/google | 1.1.0 |
| opentelemetry_collector | monte-carlo-data/otel-collector/google | 0.0.1 |

## Required Inputs

| Name | Description | Type |
|------|-------------|------|
| project_id | GCP project ID where resources will be created | `string` |
| region | GCP region where resources will be deployed | `string` |
| vpc_network | VPC network name or self-link for internal access | `string` |

## Optional Inputs

| Name | Description | Type | Default |
|------|-------------|------|---------|
| deployment_name | Name prefix for all resources | `string` | `"mcd-agent-with-otel"` |
| vpc_subnet | Subnet CIDR range for VPC connector | `string` | `null` |
| existing_vpc_connector | ID of an existing VPC connector to use | `string` | `null` |
| generate_key | Whether to generate a service account key for the Agent | `bool` | `true` |
| opentelemetry_collector_image | Docker image for the OpenTelemetry Collector | `string` | `"otel/opentelemetry-collector-contrib:latest"` |
| opentelemetry_collector_min_instances | Minimum number of Cloud Run instances | `number` | `1` |
| opentelemetry_collector_max_instances | Maximum number of Cloud Run instances | `number` | `10` |
| opentelemetry_collector_cpu | CPU allocation for Cloud Run container | `string` | `"1"` |
| opentelemetry_collector_memory | Memory allocation for Cloud Run container | `string` | `"2Gi"` |
| opentelemetry_collector_bigquery_table_id | BigQuery table ID for storing trace data | `string` | `null` |
| deletion_protection | Enable deletion protection for Cloud Run service | `bool` | `true` |

## Outputs

| Name | Description |
|------|-------------|
| agent_uri | The URL for the Monte Carlo Agent |
| agent_invoker_key | The Key file for Monte Carlo to invoke the agent |
| agent_invoker_sa | The agent invoker service account email |
| agent_storage_bucket_name | Name of the GCS bucket used by the Agent for data sampling and troubleshooting |
| opentelemetry_collector_service_url | Cloud Run service URL for the OpenTelemetry Collector |
| opentelemetry_collector_grpc_endpoint | The gRPC endpoint for the OpenTelemetry Collector |
| opentelemetry_collector_http_endpoint | The HTTP endpoint for the OpenTelemetry Collector |
| opentelemetry_collector_traces_topic_name | Name of the Pub/Sub topic for traces |
| opentelemetry_collector_traces_topic_id | Full ID of the Pub/Sub topic for traces |
| vpc_connector_id | ID of the VPC connector used by Agent and OpenTelemetry Collector |

## Architecture

### Storage

- **GCS Bucket**: Created by the Agent module for data sampling and troubleshooting
- **Pub/Sub Topic**: Created by the OpenTelemetry Collector for trace data export
- **BigQuery**: Optional integration for storing telemetry data

### VPC Connectivity

By default, this module creates a shared VPC Access Connector used by both the Agent and OpenTelemetry Collector. You can alternatively provide an existing VPC connector via the `existing_vpc_connector` variable.

## Security Features

- Service accounts with least privilege access
- VPC connector for internal network access
- Deletion protection enabled by default
- Sensitive outputs marked appropriately

## Post-Deployment Configuration

After deployment:
1. Register the Agent with Monte Carlo using the CLI or UI
2. Configure your applications to send telemetry to the OpenTelemetry Collector endpoints
3. (Optional) Set up BigQuery integration by providing `opentelemetry_collector_bigquery_table_id`

## Notes

- Setting `generate_key = true` will persist a key in the Terraform remote state. Please take appropriate measures to protect your remote state.
- The VPC connector is shared between both the Agent and OpenTelemetry Collector to optimize resource usage
- For production environments, keep `deletion_protection = true`

## License

Copyright 2023 Monte Carlo Data, Inc.

