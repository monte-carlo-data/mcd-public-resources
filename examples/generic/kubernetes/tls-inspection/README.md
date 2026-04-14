# Kubernetes + TLS Inspection

Inspect the full content of HTTPS traffic from the Monte Carlo Generic Agent using [mitmproxy](https://mitmproxy.org/). This lets you audit exactly what data the agent sends to Monte Carlo — complete URLs, request/response headers, and payloads — through a browser-based UI.

These instructions build on the [Kubernetes + MinIO](../minio/) example. See the [full documentation](https://docs.getmontecarlo.com/docs/kubernetes) for additional details.

## Prerequisites

1. A running Kubernetes cluster with the agent deployed using the [minio](../minio/) example (or equivalent).
2. `kubectl` and `helm` CLI tools installed and configured.
3. [OpenSSL](https://www.openssl.org/) installed (for generating the CA certificate).
4. The Helm chart version must support `firewallCa` and `container.extraEnv` (check the chart's README).

## Quick Start

### 1. Generate CA certificate

```bash
./generate-ca.sh
```

This creates a CA certificate that mitmproxy uses to intercept TLS connections.

### 2. Create the mitmproxy secret

```bash
kubectl create secret generic mitmproxy-ca -n mcd-agent \
  --from-file=mitmproxy-ca.pem=certs/mitmproxy-ca.pem
```

### 3. Deploy mitmproxy

```bash
kubectl apply -f mitmproxy.yaml
```

### 4. Configure and deploy the agent

Edit `values.yaml`:
- Replace `<YOUR_BACKEND_SERVICE_URL>` with the **Public endpoint** from the Monte Carlo app: **Account Information > Agent Service**.
- Replace `<PASTE_CA_CERT_PEM_HERE>` under `firewallCa.cert` with the contents of `certs/ca-cert.pem`.

Deploy (or upgrade) the agent:

```bash
helm upgrade --install mcd-agent \
  oci://registry-1.docker.io/montecarlodata/generic-agent-helm \
  --version 0.0.5 \
  -f values.yaml
```

The Helm chart automatically builds a combined CA bundle and sets `REQUESTS_CA_BUNDLE` on the agent when `firewallCa.cert` is configured.

### 5. Access the mitmproxy web UI

```bash
kubectl port-forward -n mcd-agent svc/mitmproxy 8081:8081
```

Open http://localhost:8081 in your browser and log in with password `mitmproxy`. All HTTPS requests from the agent appear in real time. Click any request to inspect:

- Full URL and HTTP method
- Request and response headers
- Request and response bodies (JSON payloads)
- Timing details

### 6. Verify

```bash
kubectl exec -n mcd-agent deploy/mcd-agent-deployment -- \
  curl -s -X POST localhost:8080/api/v1/test/reachability
```

A successful response contains `"ok": true`. You should also see the request appear in the mitmproxy web UI.

## How It Works

[mitmproxy](https://mitmproxy.org/) performs TLS interception by acting as a man-in-the-middle proxy:

1. The agent sends HTTPS requests through the proxy (configured via `HTTPS_PROXY`).
2. mitmproxy terminates the TLS connection using a dynamically generated certificate signed by the local CA.
3. mitmproxy records the full request and response (headers, body, timing).
4. mitmproxy opens a new TLS connection to the upstream server and forwards the request.

The agent trusts the local CA through the Helm chart's `firewallCa` feature, which uses an init container to merge the custom CA with system certificates into a combined bundle.

The included `stream_sse.py` addon (deployed as a ConfigMap) ensures that Server-Sent Events (SSE) responses are streamed through the proxy without buffering. Without it, mitmproxy would buffer the long-lived SSE connection and prevent the agent from receiving real-time events.

## Security Note

The CA private key (inside the `mitmproxy-ca` secret) can sign certificates for **any** domain. Keep it secure and use this setup for inspection and auditing purposes only.

> For basic connection-level logging (destination hosts and ports) without TLS interception, see the [proxy](../proxy/) example.

## Teardown

Remove mitmproxy and its secret:

```bash
kubectl delete -f mitmproxy.yaml
kubectl delete secret mitmproxy-ca -n mcd-agent
```

Delete the generated certificates:

```bash
rm -rf certs/
```

To remove the agent as well, see the [minio example teardown](../minio/#teardown).
