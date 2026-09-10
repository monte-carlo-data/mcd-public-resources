# Docker Compose + MinIO, authenticating with OAuth

Deploy the Monte Carlo Generic Agent with [Docker Compose](https://docs.docker.com/compose/) using [MinIO](https://min.io/) for S3-compatible object storage, authenticating to Monte Carlo with an OAuth client instead of a key/token pair.

The agent exchanges the client id and secret for short-lived access tokens on its own (`client_credentials` grant). Everything else matches the [key/token example](../minio/README.md).

## Prerequisites

1. [Docker](https://docs.docker.com/get-docker/) and [Docker Compose](https://docs.docker.com/compose/install/) installed.
2. An OAuth client for the agent (`client_id` and `client_secret`) from Monte Carlo — see [Create and Register a Generic Agent](https://docs.getmontecarlo.com/docs/generic-agent-platforms).

## Quick Start

### 1. Configure

Copy the example environment file and fill in your values:

```bash
cp .env.example .env
```

Edit `.env` and set:

- `BACKEND_SERVICE_URL` — in the Monte Carlo app, go to **Account Information > Agent Service** and copy the **Public endpoint**. The agent derives the OAuth token endpoint from it.
- `MINIO_ROOT_USER` and `MINIO_ROOT_PASSWORD` — credentials for MinIO.

Optionally set `AGENT_IMAGE_TAG` to pin the agent image to a specific version (e.g. `0.0.8-generic`). If unset, defaults to `latest-generic`. Optionally set `MCD_OAUTH_TOKEN_ENDPOINT` to override the derived token endpoint; a standard deployment does not need it.

Docker Compose automatically reads `.env` when you start the stack.

### 2. Create the credentials file

Create an OAuth client for your generic agent in Monte Carlo to obtain your `client_id` and `client_secret`, then:

```bash
mkdir -p secrets/integrations
cat > secrets/oauth.json << 'EOF'
{"client_id": "<YOUR_CLIENT_ID>", "client_secret": "<YOUR_CLIENT_SECRET>"}
EOF
chmod 600 secrets/oauth.json
```

The file is mounted read-only at `/etc/secrets/mcd-oauth/credentials.json`, the same path the Helm chart uses.

### 3. Start all services

```bash
docker compose up -d
```

This starts MinIO, automatically creates the storage bucket, and launches the agent.

### 4. Verify

Check that the agent is running and authenticating with OAuth:

```bash
docker compose logs -f mcd-agent
```

The log shows `Using OAuth client_credentials authentication` followed by the token endpoint, then `welcome: agent_id=...` once the backend accepts the client.

Test that the agent can communicate with the Monte Carlo platform:

```bash
curl -s -X POST http://localhost:8080/api/v1/test/reachability
```

A successful response contains `"ok": true`.

You can also browse the MinIO Console at http://localhost:9001 (log in with your configured `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD`) to inspect the storage bucket.

> **Note:** MinIO with default credentials is suitable for development and testing only. For production deployments, configure MinIO with proper credentials and TLS, or use a cloud-native storage service.

## Rotating the client secret

Write the new secret into `secrets/oauth.json` and restart the agent:

```bash
docker compose restart mcd-agent
```

Access tokens the agent already holds stay valid until they expire.

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
