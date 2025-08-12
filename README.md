# Monte Carlo Resources

Publicly available templates and other resources to assist customers with onboarding and using the platform.

## Templates

### CloudFormation

#### <ins>Monte Carlo's agent template for customer-hosted agent & object storage deployments in AWS ([source](templates/cloudformation/aws_apollo_agent.yaml))</ins>

This template deploys Monte Carlo's [containerized agent](https://hub.docker.com/r/montecarlodata/agent) on AWS
Lambda, along with storage, and roles:

<img src="references/images/aws_apollo_agent_arch.png" width="400" alt="AWS Agent High Level Architecture">

See [here](https://docs.getmontecarlo.com/docs/platform-architecture) for platform details
and [here](https://docs.getmontecarlo.com/docs/create-and-register-an-aws-agent) for how to create and register an agent
on AWS. A Terraform variant can be
found [here](https://registry.terraform.io/modules/monte-carlo-data/mcd-agent/aws/latest).

For any developers of the agent [this](examples/agent/test_execution.sh) simple script can be handy in testing basic
execution of the agent.

#### <ins>S3 Data Store for customer-hosted object storage deployments in AWS ([source](templates/cloudformation/aws_data_store.yaml))</ins>

This sample template creates a S3 bucket and assumable IAM role for the cloud with customer-hosted object storage
deployment model.

See [here](https://docs.getmontecarlo.com/docs/platform-architecture) for platform details
and [here](https://docs.getmontecarlo.com/docs/direct-connection-with-an-aws-data-store) for how to create and register
a data store on AWS.

#### <ins>Basic VPC ([source](templates/cloudformation/basic_vpc.yaml))</ins>

This template creates a VPC with 2 public and private subnets. Includes a NAT, IGW, and S3 VPCE.
Can be used to connect an Agent to a VPC for peering and/or IP whitelisting.

This [example](templates/cloudformation/aws_agent_with_basic_vpc.yaml) demonstrates how you can deploy an agent with
this connected VPC in one stack.

#### <ins>OpenTelemetry Collector ([source](templates/cloudformation/aws_otel_collector_ecs.yaml))</ins>

This template stands up the OpenTelemetry Collector stack to run in AWS ECS, exposing ports for GRPC and HTTP access. It includes the OpenTelemetry configuration to write incoming LLM agent traces, metrics, and logs to an S3 bucket. The stack also includes the creation of an IAM role to provide external read-only access the S3 bucket so the LLM traces can be ingested into your data warehouse.

This [example](templates/cloudformation/aws_agent_with_opentelemetry_collector.yaml) demonstrates how you can deploy an agent, a data store storage bucket, and the OpenTelemetry Collector in one stack.

### Terraform

#### <ins>AWS Data Store for customer-hosted object storage deployments in AWS ([source](templates/terraform/aws_data_store.tf))</ins>

This sample config file creates a S3 bucket and assumable IAM role for the cloud with customer-hosted
object storage deployment model on AWS.

Usage example (requires Terraform and the AWS CLI):
```bash
terraform init
terraform plan
terraform apply
```

Inputs:
- `monte_carlo_cloud_account_id` - The Monte Carlo AWS Account ID (default: 590183797493)
- `data_store_region` - AWS region to deploy resources (default: "us-east-1")

Outputs:
- `object_store_bucket_name` - The generated S3 bucket name
- `object_store_bucket_arn` - The S3 bucket ARN
- `object_store_role_arn` - The IAM role ARN
- `object_store_role_name` - The IAM role name
- `object_store_role_external_id` - The external ID for the role

These can be used when registering the data store with Monte Carlo.

See [here](https://docs.getmontecarlo.com/docs/platform-architecture) for platform details
and [here](https://docs.getmontecarlo.com/docs/direct-connection-with-an-aws-data-store) for how to create and register
a data store on AWS.

#### <ins>GCS Data Store for customer-hosted object storage deployments in GCP ([source](templates/terraform/gcs_data_store.tf))</ins>

This sample config file creates a GCS bucket, role, service account, and key for the cloud with customer-hosted
object storage deployment model on GCP.

Note that this will persist a key in the remote state used by Terraform. Please take appropriate measures to protect
your remote state.

See [here](https://docs.getmontecarlo.com/docs/platform-architecture) for platform details
and [here](https://docs.getmontecarlo.com/docs/direct-connection-with-a-gcp-data-store) for how to create and register
a data store on GCP.

## Development

### Local

1. Install
   the [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html), [Terraform](https://developer.hashicorp.com/terraform/install)
   and any providers.
2. Install any dependencies and pre-commit hooks
    ```
    pyenv activate mcd-iac-resources
    pip install -r requirements-dev.txt; pre-commit install
    ```

   This hook will lint all CloudFormation templates in the `templates/` directory
   using [cfn-lint](https://github.com/aws-cloudformation/cfn-lint).

Any CloudFormation templates can be created using commands
like [deploy](https://awscli.amazonaws.com/v2/documentation/api/latest/reference/cloudformation/deploy/index.html) or
the console. Similarly, any Terraform config files can be deployed
using [terraform apply](https://developer.hashicorp.com/terraform/cli/commands/apply) (and plan).

During development, you might want to configure Terraform Cloud as the backend. To do so you can add the following
snippet:

```
terraform {
  cloud {
    organization = "<org>"

    workspaces {
      name = "<workspace>"
    }
  }
}
```

This also requires you to execute `terraform login` before initializing. You will also have to set the execution mode
to "Local".

### Dev

After merging to `dev` CircleCI will lint, validate, and publish any templates or resources in the `templates/`
directory to `s3://mcd-dev-public-resources`.

Note that any files in this bucket are considered experimental and are not intended for production use.

## Releases

After merging to `main` CircleCI will publish any templates or resources in the `templates/` directory
to `s3://mcd-public-resources` (requires review, linting, validation, and approval).

## Additional Resources

| **Description**                                                     | **Link**                                                        |
|---------------------------------------------------------------------|-----------------------------------------------------------------|
| Monte Carlo's containerized agent                                   | https://github.com/monte-carlo-data/apollo-agent                |
| Monte Carlo's agent module for customer-hosted deployments in GCP   | https://github.com/monte-carlo-data/terraform-google-mcd-agent  |
| Monte Carlo's agent module for customer-hosted deployments in AWS   | https://github.com/monte-carlo-data/terraform-aws-mcd-agent     |
| Monte Carlo's agent module for customer-hosted deployments in Azure | https://github.com/monte-carlo-data/terraform-azurerm-mcd-agent |

## Scripts
The scripts that live here are scripts that are meant to interact with external resources (e.g. Databricks) that will connect with Monte Carlo, but not with Monte Carlo directly.

### Databricks

#### <ins> Add Monte Carlo Webhook Notifications([source](scripts/databricks/enable_monte_carlo_databricks_job_incidents.py))</ins>

This script interacts with your Databricks jobs by adding the configured Monte Carlo Webhook as a Notification on job failures,
and giving `CAN_VIEW` permissions to the Monte Carlo Service Principal.

## License

See [LICENSE](LICENSE) for more information.

## Security

See [SECURITY](SECURITY.md) for more information.
