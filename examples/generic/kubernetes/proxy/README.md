# Kubernetes + Forward Proxy

Route Monte Carlo Generic Agent traffic through a [Squid](http://www.squid-cache.org/) forward proxy so you can see exactly which hosts and ports the agent connects to.

These instructions build on the [Kubernetes + MinIO](../minio/) example. See the [full documentation](https://docs.getmontecarlo.com/docs/kubernetes) for additional details.

## Prerequisites

1. A running Kubernetes cluster with the agent deployed using the [minio](../minio/) example (or equivalent).
2. `kubectl` and `helm` CLI tools installed and configured.
3. The Helm chart version must support `container.extraEnv` (check the chart's README).

## Quick Start

### 1. Deploy Squid

```bash
kubectl apply -f squid.yaml
```

This creates a Squid proxy Deployment, Service, and ConfigMap in the `mcd-agent` namespace.

### 2. Configure and deploy the agent

Edit `values.yaml` and replace `<YOUR_BACKEND_SERVICE_URL>` with the **Public endpoint** from the Monte Carlo app: **Account Information > Agent Service**.

Deploy (or upgrade) the agent:

```bash
helm upgrade --install mcd-agent \
  oci://registry-1.docker.io/montecarlodata/generic-agent-helm \
  --version 0.0.5 \
  -f values.yaml
```

### 3. View proxy logs

```bash
kubectl exec -n mcd-agent deploy/squid -- tail -f /var/log/squid/access.log
```

All outbound HTTP/HTTPS connections from the agent are logged here.

### 4. Verify

```bash
kubectl exec -n mcd-agent deploy/mcd-agent-deployment -- \
  curl -s -X POST localhost:8080/api/v1/test/reachability
```

A successful response contains `"ok": true`.

## Reading the Access Logs

Squid logs every connection the agent makes. For HTTPS traffic you will see `CONNECT` entries like:

```
1718300000.000      1 10.42.0.5 TCP_TUNNEL/200 0 CONNECT artemis.getmontecarlo.com:443 - HIER_DIRECT/1.2.3.4 -
```

This tells you the agent opened a TLS tunnel to `artemis.getmontecarlo.com` on port 443. Because the traffic is encrypted, Squid only records the host and port — not the request path or body.

> **Note:** This setup provides connection-level visibility only (destination hosts and ports). If you need to inspect the actual request/response content (URLs, headers, and payloads), see the [tls-inspection](../tls-inspection/) example which uses mitmproxy with a browser-based UI.

## Teardown

Remove the Squid proxy:

```bash
kubectl delete -f squid.yaml
```

To remove the agent as well, see the [minio example teardown](../minio/#teardown).
