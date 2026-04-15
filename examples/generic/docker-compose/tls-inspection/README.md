# Docker Compose + TLS Inspection

Inspect the full content of HTTPS traffic from the Monte Carlo Generic Agent using [mitmproxy](https://mitmproxy.org/). This lets you audit exactly what data the agent sends to Monte Carlo — complete URLs, request/response headers, and payloads — through a browser-based UI.

## Prerequisites

1. [Docker](https://docs.docker.com/get-docker/) and [Docker Compose](https://docs.docker.com/compose/install/) installed.
2. [OpenSSL](https://www.openssl.org/) installed (for generating the CA certificate).
3. An agent token (`mcd_id` and `mcd_token`) from Monte Carlo — see [Create and Register a Generic Agent](https://docs.getmontecarlo.com/docs/generic-agent-platforms).

## Quick Start

### 1. Generate CA certificate

```bash
./generate-ca.sh
```

This creates a CA certificate that mitmproxy uses to intercept TLS connections. The agent is configured to trust this CA.

### 2. Configure

Edit `docker-compose.yml` and replace:

- `<YOUR_BACKEND_SERVICE_URL>` — in the Monte Carlo app, go to **Account Information > Agent Service** and copy the **Public endpoint**.
- `change-me-to-a-secure-password` — a secure password for MinIO (appears in 3 places: `mcd-agent`, `create-bucket`, and `minio` services).

### 3. Create the token file

[Register a new Generic Agent](https://docs.getmontecarlo.com/docs/generic-agent-platforms) in Monte Carlo and generate a key to obtain your `mcd_id` and `mcd_token`, then:

```bash
mkdir -p secrets/integrations
cat > secrets/token.json << 'EOF'
{"mcd_id": "<YOUR_MCD_ID>", "mcd_token": "<YOUR_MCD_TOKEN>"}
EOF
chmod 600 secrets/token.json
```

### 4. Start all services

```bash
docker compose up -d
```

This starts MinIO, creates the storage bucket, launches mitmproxy, and starts the agent with proxy settings.

### 5. Open the mitmproxy web UI

Open http://localhost:8081 in your browser and log in with password `mitmproxy` (configurable via `web_password` in `docker-compose.yml`). All HTTPS requests from the agent appear in real time. Click any request to inspect:

- Full URL and HTTP method
- Request and response headers
- Request and response bodies (JSON payloads)
- Timing details

### 6. Verify

Test that the agent is running and can communicate through the proxy:

```bash
curl -s -X POST http://localhost:8080/api/v1/test/reachability
```

A successful response contains `"ok": true`. You should also see the request appear in the mitmproxy web UI.

## How It Works

[mitmproxy](https://mitmproxy.org/) performs TLS interception by acting as a man-in-the-middle proxy:

1. The agent sends HTTPS requests through the proxy.
2. mitmproxy terminates the TLS connection using a dynamically generated certificate signed by the local CA.
3. mitmproxy records the full request and response (headers, body, timing).
4. mitmproxy opens a new TLS connection to the upstream server and forwards the request.

The agent trusts the local CA because `mitmproxy-ca-cert.pem` is mounted into its certificate store and `update-ca-certificates` is run at startup. The `REQUESTS_CA_BUNDLE` environment variable ensures Python's `requests` library also uses the updated store.

The included `stream_sse.py` addon ensures that Server-Sent Events (SSE) responses are streamed through the proxy without buffering. Without it, mitmproxy would buffer the long-lived SSE connection and prevent the agent from receiving real-time events.

## Security Note

The CA private key (`certs/mitmproxy-ca.pem`) can sign certificates for **any** domain. Keep it secure and use this setup for inspection and auditing purposes only. Do not distribute the CA key outside your environment.

> For basic connection-level logging (destination hosts and ports) without TLS interception, see the [proxy](../proxy/) example.

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

This removes all containers, volumes, and networks. The generated CA certificate in `certs/` is not removed automatically — delete it manually if no longer needed:

```bash
rm -rf certs/
```
