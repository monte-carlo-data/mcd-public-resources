# Monte Carlo IaC Resources

Publicly available templates and other resources to assist customers with onboarding and using the platform.

## Templates

### CloudFormation

#### Monte Carlo's agent template for customer-hosted deployments in AWS ([Source](templates/cloudformation/aws_apollo_agent.yaml))

This template deploys Monte Carlo's [containerized agent](https://hub.docker.com/r/montecarlodata/agent) (Beta) on AWS
Lambda, along with storage, and roles:

<img src="references/images/aws_apollo_agent_arch.png" width="400" alt="AWS Agent High Level Architecture">

See [here](https://docs.getmontecarlo.com/docs/platform-architecture) for platform details
and [here](https://docs.getmontecarlo.com/docs/create-and-register-an-aws-agent) for how to create and register an agent
on AWS.

For any developers of the agent [this](examples/agent/test_execution.sh) simple script can be handy in testing basic
execution of the agent.

### Terraform

Coming soon!

## Development

### CloudFormation

1. Install the [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)
2. Install any dependencies and pre-commit hooks
    ```
    pyenv activate mcd-iac-resources
    pip install -r requirements-dev.txt; pre-commit install
    ```
   This hook will lint all templates in the `templates/` directory
   using [cfn-lint](https://github.com/aws-cloudformation/cfn-lint).

Then any IaC templates can be created using commands
like [deploy](https://awscli.amazonaws.com/v2/documentation/api/latest/reference/cloudformation/deploy/index.html) or
the Console.

After merging to `dev` CircleCI will publish any templates or resources in the `templates/` directory
to `s3://mcd-dev-public-resources`.

Note that any templates in this bucket are considered experimental and not intended for production use.

### Terraform

Coming soon!

## Releases

After merging to `main` CircleCI will publish any templates or resources in the `templates/` directory
to `s3://mcd-public-resources` (requires review and approval).

## Additional Resources

| **Description**                                                   | **Link**                                                       |
|-------------------------------------------------------------------|----------------------------------------------------------------|
| Monte Carlo's containerized agent                                 | https://github.com/monte-carlo-data/apollo-agent               |
| Monte Carlo's agent module for customer-hosted deployments in GCP | https://github.com/monte-carlo-data/terraform-google-mcd-agent |

## License

See [LICENSE](LICENSE) for more information.

## Security

See [SECURITY](SECURITY.md) for more information.