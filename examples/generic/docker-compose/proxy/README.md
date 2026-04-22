# Docker Compose + Forward Proxy

Route Monte Carlo Generic Agent traffic through a [Squid](http://www.squid-cache.org/) forward proxy so you can see exactly which hosts and ports the agent connects to.

## Prerequisites

1. [Docker](https://docs.docker.com/get-docker/) and [Docker Compose](https://docs.docker.com/compose/install/) installed.
2. An agent token (`mcd_id` and `mcd_token`) from Monte Carlo — see [Create and Register a Generic Agent](https://docs.getmontecarlo.com/docs/generic-agent-platforms).

## Quick Start

### 1. Configure

Copy the example environment file and fill in your values:

```bash
cp .env.example .env
```

Edit `.env` and set:

- `BACKEND_SERVICE_URL` — in the Monte Carlo app, go to **Account Information > Agent Service** and copy the **Public endpoint**.
- `MINIO_ROOT_USER` and `MINIO_ROOT_PASSWORD` — credentials for MinIO.

Docker Compose automatically reads `.env` when you start the stack.

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

This starts the Squid proxy, MinIO, automatically creates the storage bucket, and launches the agent with `HTTP_PROXY` and `HTTPS_PROXY` pointed at Squid.

### 4. View proxy logs

```bash
docker compose exec squid tail -f /var/log/squid/access.log
```

All outbound HTTP/HTTPS connections from the agent are logged here.

### 5. Verify

Check that the agent is running and can reach Monte Carlo through the proxy:

```bash
curl -s -X POST http://localhost:8080/api/v1/test/reachability
```

A successful response contains `"ok": true`.

## Reading the Access Logs

Squid logs every connection the agent makes. For HTTPS traffic you will see `CONNECT` entries like:

```
1718300000.000      1 172.18.0.4 TCP_TUNNEL/200 0 CONNECT artemis.getmontecarlo.com:443 - HIER_DIRECT/1.2.3.4 -
```

This tells you the agent opened a TLS tunnel to `artemis.getmontecarlo.com` on port 443. Because the traffic is encrypted, Squid only records the host and port — not the request path or body.

> **Note:** This setup provides connection-level visibility only (destination hosts and ports). If you need to inspect the actual request/response content (URLs, headers, and payloads), see the [tls-inspection](../tls-inspection/) example which uses mitmproxy with a browser-based UI.

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
