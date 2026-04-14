# Kubernetes + MinIO

Deploy the Monte Carlo Generic Agent on Kubernetes using [Helm](https://helm.sh/) with [MinIO](https://min.io/) for S3-compatible object storage.

These instructions are platform-agnostic and work on any Kubernetes distribution (EKS, AKS, GKE, k3s, kind, minikube, or on-premises). See the [full documentation](https://docs.getmontecarlo.com/docs/kubernetes) for additional details.

## Prerequisites

1. A running Kubernetes cluster (any distribution).
2. `kubectl` and `helm` CLI tools installed and configured to access your cluster.
3. An agent token (`mcd_id` and `mcd_token`) from Monte Carlo — see [Create and Register a Generic Agent](https://docs.getmontecarlo.com/docs/generic-agent-platforms).

## Quick Start

### 1. Deploy MinIO

```bash
kubectl apply -f https://raw.githubusercontent.com/monte-carlo-data/hermes-agent/main/environments/local/minio/k8s.yaml
```

Wait for MinIO to be ready, then create the storage bucket:

```bash
kubectl port-forward -n minio deploy/minio 9000:9000 9001:9001
```

Open http://localhost:9001, log in with `minioadmin` / `minioadmin`, and create a bucket called `mcd-agent-storage`.

> **Note:** MinIO with default credentials is suitable for development and testing only. For production deployments, use a cloud-native storage service or configure MinIO with proper credentials, TLS, and persistent storage.

### 2. Create the agent namespace

```bash
kubectl create namespace mcd-agent
kubectl label namespace mcd-agent app.kubernetes.io/managed-by=Helm
kubectl annotate namespace mcd-agent meta.helm.sh/release-name=mcd-agent meta.helm.sh/release-namespace=default
```

### 3. Create secrets

[Register a new Generic Agent](https://docs.getmontecarlo.com/docs/generic-agent-platforms) in Monte Carlo and generate a key to obtain your `mcd_id` and `mcd_token`.

Create the agent token secret:

```bash
cp secrets/token.json.example secrets/token.json
# Edit secrets/token.json with your mcd_id and mcd_token
kubectl create secret generic mcd-agent-token-secret -n mcd-agent \
  --from-file=contents.json=secrets/token.json
```

Create the integrations secret (can start empty and be populated later):

```bash
kubectl create secret generic mcd-integrations-secrets -n mcd-agent \
  --from-file=empty.json=<(echo '{}')
```

### 4. Configure and deploy

Edit `values.yaml` and replace `<YOUR_BACKEND_SERVICE_URL>` with the **Public endpoint** from the Monte Carlo app: **Account Information > Agent Service**.

Deploy the agent:

```bash
helm upgrade --install mcd-agent \
  oci://registry-1.docker.io/montecarlodata/generic-agent-helm \
  --version 0.0.5 \
  -f values.yaml
```

> Check [Docker Hub](https://hub.docker.com/r/montecarlodata/generic-agent-helm/tags) for the latest chart version.

### 5. Verify

Check the agent pod is running:

```bash
kubectl get pods -n mcd-agent
kubectl logs -n mcd-agent -l app=mcd-agent --tail=30
```

Test that the agent can communicate with the Monte Carlo platform:

```bash
kubectl exec -n mcd-agent deploy/mcd-agent-deployment -- \
  curl -s -X POST localhost:8080/api/v1/test/reachability
```

A successful response contains `"ok": true`.

## Adding Integration Credentials

Replace the integrations secret with one containing your connection details:

```bash
kubectl delete secret mcd-integrations-secrets -n mcd-agent
kubectl create secret generic mcd-integrations-secrets -n mcd-agent \
  --from-file=<integration>.json=secrets/integrations/<integration>.json
kubectl rollout restart deployment/mcd-agent-deployment -n mcd-agent
```

You can include multiple integration files using additional `--from-file` flags. See the [Self-Hosted Credentials](https://docs.getmontecarlo.com/docs/self-hosted-credentials) documentation for the JSON format for each integration type.

Then register the integration in Monte Carlo using the CLI:

```bash
montecarlo integrations add-self-hosted-credentials-v2 \
  --connection-type <integration> \
  --self-hosted-credentials-type FILE \
  --file-path /etc/secrets/integrations/<integration>.json \
  --name <connection_name>
```

## Updating the Agent

Update the `--version` flag and `image.tag` in `values.yaml`, then re-run the `helm upgrade` command.

## Teardown

```bash
helm uninstall mcd-agent
kubectl delete namespace mcd-agent
kubectl delete -f https://raw.githubusercontent.com/monte-carlo-data/hermes-agent/main/environments/local/minio/k8s.yaml
```
