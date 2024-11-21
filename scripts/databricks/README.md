# Databricks Scripts

## Installation

In a Python 3.11+ environment

```
pip install -r requirements.txt
```

## <ins> Add Monte Carlo Webhook Notifications([source](enable_monte_carlo_databricks_job_incidents.py))</ins>

This script interacts with your Databricks jobs by adding the configured Monte Carlo Webhook as a Notification on job failures,
and giving `CAN_VIEW` permissions to the Monte Carlo Service Principal.

### Usage

```
Usage: enable_monte_carlo_databricks_job_incidents.py [OPTIONS]

  Enable Monte Carlo incidents for Databricks jobs

Options:
  --mcd-notification-id TEXT      UUID of the existing Databricks Notification
                                  pointing to the MC Webhook endpoint.
                                  [required]
  --mcd-service-principal-name TEXT
                                  Application ID of the existing Monte Carlo
                                  service principal in Databricks.  [required]
  --databricks-job-name TEXT      Databricks Job Name to enable MC incidents
                                  for. Can be used multiple times. If not
                                  specified, enable MC incidents for all jobs.
  --help                          Show this message and exit.
```

You can get the UUID for your Monte Carlo Webhook in Databricks from
```
https://<your-databricks-workspace>/settings/workspace/notifications/notification-destinations
```

and by clicking the Monte Carlo Webhook notification, and clickng on `Copy destination ID`

<img width="655" alt="Screenshot 2024-11-18 at 3 41 39 PM" src="https://github.com/user-attachments/assets/b51f852f-834c-4eeb-b81b-d9d64d10587e">

You can get the Application ID for the Monte Carlo Service Principal in Databricks from 
```
https://<your-databricks-workspace>/settings/workspace/identity-and-access/service-principals
```

You can get the Job Names from the Databricks Workflows page
```
https://<your-databricks-workspace>/jobs
```

### Example
```
# Create and source a Virtual Environment
python3 -m venv ./mcd-public-resources
source ./mcd-public-resources/bin/activate

# Install the python requirements
pip install -r requirements.txt

# Run the script
python enable_monte_carlo_databricks_job_incidents.py --mcd-notification-id '<mcd-notification-uuid>' --mcd-service-principal-name '<mcd-service-principal-application-id>'

