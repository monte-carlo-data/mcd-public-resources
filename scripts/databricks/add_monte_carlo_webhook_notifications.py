from typing import Set

import click
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.jobs import Webhook, WebhookNotifications


@click.command(help="Add the Monte Carlo webhook to Databricks jobs")
@click.pass_obj
@click.option(
    "--mcd-notification-id",
    required=True,
    help="UUID of the existing Databricks Notification pointing to the MC Webhook endpoint.",
)
@click.option(
    "--databricks-job-name",
    required=False,
    multiple=True,
    help="Databricks Job Name to add the MC Webhook to. Can be used multiple times. If not specified, add the MC Webhook to all jobs.",
)
def add_monte_carlo_webhook_notifications(ctx, **kwargs):
    """
    Add the Monte Carlo Webhook to notify on Job Failures
    """
    databricks_job_names = set(kwargs["databricks_job_name"])
    _add_monte_carlo_webhook_notifications(
        mc_notification_id=kwargs["mc_notification_id"], databricks_job_names=databricks_job_names
    )


def _add_monte_carlo_webhook_notifications(
    mc_notification_id: str, databricks_job_names: Set[str]
):
    workspace_client = WorkspaceClient()

    # assert that the notification exists
    workspace_client.notification_destinations.get(id=mc_notification_id)
    mc_notification = Webhook(id=mc_notification_id)

    jobs = workspace_client.jobs.list()

    click.echo(
        f"Configuring the Monte Carlo Webhook for {len(jobs)} jobs"
    )

    for job in jobs:
        job_name = job.settings.name
        if databricks_job_names and job.settings.name not in databricks_job_names:
            continue

        existing_settings = job.settings
        existing_webhooks = existing_settings.webhook_notifications
        if existing_webhooks is None:
            updated_webhooks = WebhookNotifications(on_failure=[mc_notification])
        else:
            updated_webhooks = existing_webhooks
            if mc_notification not in updated_webhooks.on_failure:
                updated_webhooks.on_failure.append(mc_notification)
            else:
                click.echo(
                    f"The Monte Carlo webhook is already configured for {job_name}"
                )
                continue

        new_settings = existing_settings
        new_settings.webhook_notifications = updated_webhooks

        try:
            workspace_client.jobs.update(job_id=job.job_id, new_settings=new_settings)
            click.echo(f"Successfully added the Monte Carlo webhook to {job_name}")
        except Exception as e:
            click.echo(
                f"Failed to add the Monte Carlo Webhook to {job_name} due to {str(e)}", err=True
            )


if __name__ == "__main__":
    add_monte_carlo_webhook_notifications()
