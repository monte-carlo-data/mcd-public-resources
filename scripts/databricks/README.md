# Databricks Scripts

## Installation

In a Python 3.11+ environment

```
pip install -r requirements.txt
```

## <ins> Add Monte Carlo Webhook Notifications([source](scripts/databricks/add_monte_carlo_webhook_notifications.py))</ins>

This script interacts with your Databricks jobs and adds the configured Monte Carlo Webhook as a Notification on job failures.

### Usage

```
Usage: add_monte_carlo_webhook_notifications.py [OPTIONS]

  Add the Monte Carlo webhook to Databricks jobs

Options:
  --mcd-notification-id TEXT  UUID of the existing Databricks Notification
                              pointing to the MC Webhook endpoint.  [required]
  --databricks-job-name TEXT  Databricks Job Name to add the MC Webhook to.
                              Can be used multiple times. If not specified,
                              add the MC Webhook to all jobs.
  --help                      Show this message and exit.
```

You can get the UUID for your Monte Carlo Webhook in Databricks from
```
https://<your-databricks-workspace>/settings/workspace/notifications/notification-destinations
```

and by clicking the Monte Carlo Webhook notification, and clickng on `Copy destination ID`

<img width="655" alt="Screenshot 2024-11-18 at 3 41 39 PM" src="https://github.com/user-attachments/assets/b51f852f-834c-4eeb-b81b-d9d64d10587e">

You can get the Job Names from the Databricks Workflows page

```
https://<your-databricks-workspace>/jobs
```
