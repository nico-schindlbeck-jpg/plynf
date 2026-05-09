# Plinth Helm chart

This chart deploys the full Plinth stack (Workspace, Gateway, Identity,
Dashboard, and the bundled MCP servers) onto a Kubernetes 1.27+ cluster.

It is the recommended way to run Plinth in any environment more permanent
than `docker compose`.

## Quick start

```bash
# 1. Generate a JWT secret out-of-band.
openssl rand -hex 48 > /tmp/plinth-jwt.txt

# 2. Create the namespace.
kubectl create namespace plinth

# 3. Install the chart with the dev values.
helm install plinth ./deploy/helm/plinth \
  --namespace plinth \
  -f ./deploy/helm/plinth/values.yaml \
  -f ./deploy/helm/plinth/values.dev.yaml \
  --set-file identity.jwtSecret=/tmp/plinth-jwt.txt

# 4. Open the dashboard.
kubectl -n plinth port-forward svc/plinth-dashboard 7424:7424
open http://localhost:7424
```

## Production

For a real deployment use `values.prod.yaml` and supply secrets out-of-band
(SealedSecrets, External Secrets Operator, AWS Secrets Manager via Secrets
Store CSI, etc.).

```bash
helm upgrade --install plinth ./deploy/helm/plinth \
  --namespace plinth --create-namespace \
  -f ./deploy/helm/plinth/values.yaml \
  -f ./deploy/helm/plinth/values.prod.yaml \
  --set-file identity.jwtSecret=./secrets/jwt.txt \
  --set-file gateway.oauth.encryptionKey=./secrets/oauth-key.txt \
  --set workspace.postgresUrl="postgresql+psycopg://plinth:***@db.example.com:5432/plinth"
```

If you manage the application Secret yourself, set `existingSecret: my-secret`
to skip the chart's templated Secret. The Secret must contain the keys listed
in `deploy/k8s/secrets.example.yaml`.

## Values reference

The full reference lives in `values.yaml`; the highlights:

| Key | Default | Description |
| --- | --- | --- |
| `global.imageRegistry` | `ghcr.io/nico-schindlbeck-jpg/plinth` | OCI registry prefix. |
| `global.imageTag` | `1.0.0` | Default image tag. Per-service overrides via `<svc>.image.tag`. |
| `global.replicationMode` | `standalone` | `standalone`, `primary`, or `replica`. |
| `global.region` | `""` | Sets `PLINTH_REGION_ID` on every service. |
| `workspace.persistence.size` | `10Gi` | Storage for SQLite data dir. |
| `workspace.postgresUrl` | `""` | Optional Postgres URL. When set, replaces SQLite. |
| `gateway.oauth.enabled` | `false` | When true, requires `gateway.oauth.encryptionKey`. |
| `gateway.hpa.enabled` | `false` | Horizontal Pod Autoscaler. Recommended for prod. |
| `gateway.pdb.enabled` | `false` | Pod Disruption Budget. Recommended for prod. |
| `identity.jwtSecret` | `""` | **Required** unless `existingSecret` is set. |
| `dashboard.ingress.enabled` | `false` | Expose dashboard via Ingress. |
| `ingress.enabled` | `false` | Master switch for the Ingress umbrella. |
| `ingress.gateway.enabled` | `false` | Expose gateway via Ingress. |
| `networkPolicy.enabled` | `false` | Default-deny + allow-internal NetworkPolicies. |
| `mockMcp.enabled` | `true` | Disable in prod (`values.prod.yaml` does this). |
| `githubMcp.enabled` | `false` | Enable per-tenant by setting OAuth creds in Secret. |
| `slackMcp.enabled` | `false` | Same as github. |
| `linearMcp.enabled` | `false` | Same as github. |
| `postgres.enabled` | `false` | In-cluster single-node Postgres. Dev only. |

## Upgrades

```bash
helm upgrade plinth ./deploy/helm/plinth \
  --namespace plinth \
  -f ./deploy/helm/plinth/values.yaml \
  -f ./deploy/helm/plinth/values.prod.yaml \
  --reuse-values \
  --set global.imageTag=1.1.0
```

Plinth services run database migrations on startup (`auto_migrate: true`).
For zero-downtime upgrades, schedule the upgrade during low traffic and
verify the readiness probe passes before pulling the trigger on a rolling
restart.

## Uninstall

```bash
helm uninstall plinth --namespace plinth
kubectl delete pvc --all --namespace plinth   # only if you want the data gone
kubectl delete namespace plinth
```

## See also

- `docs/deployment.md` — operator handbook (sizing, secrets, backups, troubleshooting)
- `docs/API_STABILITY.md` — API v1 stability promise
- `deploy/k8s/` — raw Kustomize manifests if you prefer those over Helm
- `deploy/terraform/aws-example/` — Terraform starter for AWS EKS
