# Docker Compose + MinIO

Deploy the Monte Carlo Generic Agent with [Docker Compose](https://docs.docker.com/compose/) using [MinIO](https://min.io/) for S3-compatible object storage.

## Prerequisites

1. [Docker](https://docs.docker.com/get-docker/) and [Docker Compose](https://docs.docker.com/compose/install/) installed.
2. An agent token (`mcd_id` and `mcd_token`) from Monte Carlo — see [Create and Register a Generic Agent](https://docs.getmontecarlo.com/docs/generic-agent-platforms).

## Quick Start

### 1. Configure

Edit `docker-compose.yml` and replace:

- `<YOUR_BACKEND_SERVICE_URL>` — in the Monte Carlo app, go to **Account Information > Agent Service** and copy the **Public endpoint**.
- `change-me-to-a-secure-password` — a secure password for MinIO (appears in 3 places: `mcd-agent`, `create-bucket`, and `minio` services).

### 2. Create the token file

[Register a new Generic Agent](https://docs.getmontecarlo.com/docs/generic-agent-platforms) in Monte Carlo and generate a key to obtain your `mcd_id` and `mcd_token`, then:

```bash
mkdir -p secrets/integrations
cat > secrets/token.json << 'EOF'
{"mcd_id": "<YOUR_MCD_ID>", "mcd_token": "<YOUR_MCD_TOKEN>"}
EOF
chmod 600 secrets/token.json
```

### 3. Start all services

```bash
docker compose up -d
```

This starts MinIO, automatically creates the storage bucket, and launches the agent.

### 4. Verify

Check that the agent is running:

```bash
docker compose logs -f mcd-agent
```

Test that the agent can communicate with the Monte Carlo platform:

```bash
curl -s -X POST http://localhost:8080/api/v1/test/reachability
```

A successful response contains `"ok": true`.

You can also browse the MinIO Console at http://localhost:9001 (log in with `minioadmin` and your configured password) to inspect the storage bucket.

> **Note:** MinIO with default credentials is suitable for development and testing only. For production deployments, configure MinIO with proper credentials and TLS, or use a cloud-native storage service.

## Adding Integration Credentials

Place integration credential files in `./secrets/integrations/`. They are mounted read-only into the container at `/etc/secrets/integrations/`.

See the [Self-Hosted Credentials](https://docs.getmontecarlo.com/docs/self-hosted-credentials) documentation for the JSON format for each integration type.

After adding the files, restart the agent:

```bash
docker compose restart mcd-agent
```

Then register the integration in Monte Carlo using the CLI:

```bash
montecarlo integrations add-self-hosted-credentials-v2 \
  --connection-type <integration> \
  --self-hosted-credentials-type FILE \
  --file-path /etc/secrets/integrations/<integration>.json \
  --name <connection_name>
```

## Teardown

```bash
docker compose down -v
```
