from typing import Set

import click
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.jobs import Webhook, WebhookNotifications, Job, JobAccessControlRequest, JobPermissionLevel
from databricks.sdk.service.iam import ServicePrincipal


@click.command(help="Enable Monte Carlo incidents for Databricks jobs")
@click.pass_obj
@click.option(
    "--mcd-notification-id",
    required=True,
    help="UUID of the existing Databricks Notification pointing to the MC Webhook endpoint.",
)
@click.option(
    "--mcd-service-principal-name",
    required=True,
    help="Application ID of the existing Monte Carlo service principal in Databricks.",
)
@click.option(
    "--databricks-job-name",
    required=False,
    multiple=True,
    help="Databricks Job Name to enable MC incidents for. Can be used multiple times. If not specified, enable MC incidents for all jobs.",
)
def enable_monte_carlo_databricks_job_incidents(ctx, **kwargs):
    """
    Add the Monte Carlo Webhook to notify on Job Failures
    """
    databricks_job_names = set(kwargs["databricks_job_name"])
    _enable_monte_carlo_databricks_job_incidents(
        mcd_notification_id=kwargs["mcd_notification_id"],
        mcd_service_principal_name=kwargs["mcd_service_principal_name"],
        databricks_job_names=databricks_job_names,
    )


def _enable_monte_carlo_databricks_job_incidents(
    mcd_notification_id: str,
    mcd_service_principal_name: str,
    databricks_job_names: Set[str],
):
    workspace_client = WorkspaceClient()

    # assert that the notification exists
    workspace_client.notification_destinations.get(id=mcd_notification_id)
    mcd_notification = Webhook(id=mcd_notification_id)

    # assert that the service principal exists
    mcd_service_principal = None
    service_principals = workspace_client.service_principals.list()
    for service_principal in service_principals:
        if service_principal.application_id == mcd_service_principal_name:
            mcd_service_principal = service_principal
            break

    assert mcd_service_principal is not None

    jobs = list(workspace_client.jobs.list())

    click.echo(
        f"Configuring the Monte Carlo webhook for {len(jobs)} jobs"
    )

    for job in jobs:
        job_name = job.settings.name
        if databricks_job_names and job.settings.name not in databricks_job_names:
            continue

        _add_monte_carlo_webhook_to_job(workspace_client, job, mcd_notification)
        _add_can_view_permissions(workspace_client, job, mcd_service_principal)

def _add_monte_carlo_webhook_to_job(workspace_client: WorkspaceClient, job: Job, mcd_notification: Webhook):
    job_name = job.settings.name
    existing_settings = job.settings
    existing_webhooks = existing_settings.webhook_notifications
    if existing_webhooks is None:
        updated_webhooks = WebhookNotifications(on_failure=[mcd_notification])
    else:
        updated_webhooks = existing_webhooks
        if mcd_notification not in updated_webhooks.on_failure:
            updated_webhooks.on_failure.append(mcd_notification)
        else:
            click.echo(
                f"The Monte Carlo webhook is already configured for {job_name}"
            )
            return

    new_settings = existing_settings
    new_settings.webhook_notifications = updated_webhooks

    try:
        workspace_client.jobs.update(job_id=job.job_id, new_settings=new_settings)
        click.echo(f"Successfully added the Monte Carlo webhook to {job_name}")
    except Exception as e:
        click.echo(
            f"Failed to add the Monte Carlo webhook to {job_name} due to {str(e)}", err=True
        )

def _add_can_view_permissions(workspace_client: WorkspaceClient, job: Job, mcd_service_principal: ServicePrincipal):
    job_name = job.settings.name
    permission_to_add = JobAccessControlRequest(
        service_principal_name=mcd_service_principal.application_id,
        permission_level=JobPermissionLevel.CAN_VIEW,
    )

    try:
        workspace_client.jobs.update_permissions(job_id=job.job_id, access_control_list=[permission_to_add])
        click.echo(
            f"Successfully gave Can View permissions to the Monte Carlo Service Principal for {job_name}"
        )
    except Exception as e:
        click.echo(
            f"Failed to give Can Veiw permissions the Monte Carlo Service Principal for {job_name} due to {str(e)}", err=True
        )


if __name__ == "__main__":
    enable_monte_carlo_databricks_job_incidents()
