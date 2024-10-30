from typing import Set

import click
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.jobs import Webhook, WebhookNotifications


@click.command(help="Add the monte carlo webhook to Databricks jobs")
@click.pass_obj
@click.option(
    "--mc-notification-id",
    required=True,
    help="UUID of the existing Databricks Notification pointing to the MC Webhook endpoint",
)
@click.option(
    "--job-name",
    required=False,
    multiple=True,
    help="Job Name to add the MC Webhook to. Can be used multiple times. If not specified, add the MC Webhook to all jobs.",
)
def add_monte_carlo_webhook_notifications(ctx, **kwargs):
    job_names = set(kwargs["job_name"])
    _add_monte_carlo_webhook_notifications(
        mc_notification_id=kwargs["mc_notification_id"], job_names=job_names
    )


def _add_monte_carlo_webhook_notifications(
    mc_notification_id: str, job_names: Set[str]
):
    w = WorkspaceClient()

    # assert that the notification exists
    w.notification_destinations.get(id=mc_notification_id)
    mc_notification = Webhook(id=mc_notification_id)

    for job in w.jobs.list():
        job_name = job.settings.name
        if job_names and job.settings.name not in job_names:
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
                    f"The monte carlo webhook is already configured for {job_name}"
                )
                continue

        new_settings = existing_settings
        new_settings.webhook_notifications = updated_webhooks

        try:
            w.jobs.update(job_id=job.job_id, new_settings=new_settings)
            click.echo(f"Successfully added the monte carlo webhook to {job_name}")
        except Exception as e:
            click.echo(
                f"Failed to add the monte carlo webhook to {job_name} due to {str(e)}"
            )


if __name__ == "__main__":
    add_monte_carlo_webhook_notifications()
