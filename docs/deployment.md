# Plynf — Deployment Handbook

> **Audience**: Operators standing up Plynf in dev, staging, or production.
> **Status**: Active as of v1.0.0 GA.

This document covers how to install, upgrade, back up, and troubleshoot a
Plynf deployment. Three deployment shapes are supported in tree:

1. **Docker Compose** — laptop / single-node demos.
2. **Kubernetes manifests** (`deploy/k8s/`) — bring-your-own-Kustomize.
3. **Helm chart** (`deploy/helm/plinth/`) — recommended.

A starter Terraform module for AWS sits in `deploy/terraform/aws-example/`.
It is a starting point, not a turnkey production module.

## Architecture recap

A Plynf install consists of four core services plus optional MCP servers:

| Service | Port | Stateful? | Scales horizontally? |
| --- | --- | --- | --- |
| `workspace` | 7421 | Yes (PVC or Postgres) | Only with Postgres backend |
| `gateway` | 7422 | Yes (PVC or Postgres) | Yes |
| `identity` | 7425 | Yes (PVC or Postgres) | Only with Postgres backend |
| `dashboard` | 7424 | No | Yes |
| `mock-mcp` | 7423 | No | Demo only |
| `github-mcp` | 7426 | No | Yes |
| `slack-mcp` | 7427 | No | Yes |
| `linear-mcp` | 7428 | No | Yes |

All services expose `/healthz` (liveness + readiness), `/metrics`
(Prometheus), and serve their JSON API on the port above.

## Deployment shape 1: Docker Compose

For laptops, demos, and quick smoke tests:

```bash
git clone https://github.com/nico-schindlbeck-jpg/plinth
cd plinth
PLINTH_IDENTITY_JWT_SECRET="$(openssl rand -hex 48)" docker compose up --build -d
docker compose logs -f workspace
open http://localhost:7424   # dashboard
```

Compose ships SQLite-backed data on a shared volume. State persists across
restarts; `docker compose down -v` wipes it.

## Deployment shape 2: Kubernetes (Kustomize)

For self-managed clusters that prefer raw manifests:

```bash
# Create a namespace and a real Secret out-of-band.
kubectl create namespace plinth
kubectl -n plinth create secret generic plinth-secrets \
  --from-literal=jwt-secret="$(openssl rand -hex 48)" \
  --from-literal=oauth-encryption-key="$(openssl rand -hex 32)" \
  --from-literal=postgres-url=""

# Apply the dev overlay.
kubectl apply -k deploy/k8s/overlays/dev

# Or the prod overlay.
kubectl apply -k deploy/k8s/overlays/prod
```

The base kustomization (`deploy/k8s/kustomization.yaml`) ships everything
except Postgres (opt-in via the prod overlay) and the example Secret
(template only — never apply it as-is).

## Deployment shape 3: Helm

Recommended for any non-trivial install. Installs the same set of resources
as the Kustomize manifests, but exposes them via `values.yaml` so you can
toggle replicas, HPA, Ingress, NetworkPolicy, and so on without forking
files.

```bash
helm upgrade --install plinth ./deploy/helm/plinth \
  --namespace plinth --create-namespace \
  -f ./deploy/helm/plinth/values.yaml \
  -f ./deploy/helm/plinth/values.prod.yaml \
  --set-file identity.jwtSecret=./secrets/jwt.txt \
  --set-file gateway.oauth.encryptionKey=./secrets/oauth-key.txt \
  --set workspace.postgresUrl="postgresql+psycopg://plinth:***@db.example.com:5432/plinth"
```

See `deploy/helm/plinth/README.md` for the full values reference.

## Sizing recommendations

These are starting points, not benchmarks. Tune from observed CPU + memory
metrics; the dashboard ships with a 24h time-series view that's the right
place to start.

### Single-tenant / demo / staging

| Service | Replicas | CPU req / lim | Memory req / lim |
| --- | --- | --- | --- |
| workspace | 1 | 100m / 500m | 256Mi / 512Mi |
| gateway | 1 | 100m / 500m | 256Mi / 512Mi |
| identity | 1 | 50m / 250m | 128Mi / 256Mi |
| dashboard | 1 | 50m / 250m | 128Mi / 256Mi |
| each MCP | 1 | 25m / 100m | 64Mi / 128Mi |

### Production (Postgres-backed, ~100 tenants)

| Service | Replicas | CPU req / lim | Memory req / lim |
| --- | --- | --- | --- |
| workspace | 1* | 250m / 2 cores | 512Mi / 2Gi |
| gateway | 3, HPA to 10 | 250m / 2 cores | 512Mi / 2Gi |
| identity | 1* | 100m / 1 core | 256Mi / 1Gi |
| dashboard | 3 | 100m / 500m | 256Mi / 512Mi |

`*` Workspace and Identity are single-writer for SQLite. With Postgres they
scale horizontally — bump replicas to match the gateway.

## Database options

Plynf speaks two backends: SQLite (default, embedded) and Postgres
(required for HA + horizontal scaling).

### SQLite (dev, small prod)

- Zero external dependency.
- One pod per service. Recreate strategy on rolling updates (so the writer
  pod releases the file before the next one mounts it).
- PVC-backed; back up the PVC volume snapshots.

### Postgres (prod, multi-replica)

- Required for `workspace.replicas > 1` and `identity.replicas > 1`.
- Set the `postgres-url` key in the application Secret. Format:
  `postgresql+psycopg://user:pass@host:5432/plinth`.
- Each service auto-migrates its schema on startup. You can preview pending
  migrations with `plinth migrate <svc> --status`.
- Use a managed Postgres (RDS, Cloud SQL, Aiven). The included
  `deploy/k8s/postgres.yaml` and `postgres.enabled=true` Helm path are for
  dev / starter installs only.

## Secret management

Plynf needs three classes of secret:

1. **`jwt-secret`** — HS256 signing secret used by Identity. Required.
   Generate with `openssl rand -base64 48 | tr -d '\n'`. Rotate every 90
   days; identity supports a `kid` header so JWTs signed with the previous
   secret keep verifying for a configurable overlap window.
2. **`oauth-encryption-key`** — AES-GCM key used by Gateway to encrypt OAuth
   tokens at rest. Required when `gateway.oauth.enabled=true`. 32 bytes
   base64-encoded.
3. **OAuth client credentials** — `<provider>-oauth-client-id` and
   `<provider>-oauth-client-secret` for each provider you've enabled.

### Recommended workflow

| Environment | Secret backend |
| --- | --- |
| Laptop / Compose | `.env` file (gitignored). |
| Self-managed k8s | [SealedSecrets](https://github.com/bitnami-labs/sealed-secrets). |
| AWS | AWS Secrets Manager + [External Secrets Operator](https://external-secrets.io/). |
| GCP | Secret Manager + ESO. |
| Azure | Key Vault + ESO. |

When using ESO, set `existingSecret: my-eso-secret` on the Helm chart so it
skips the templated Secret resource.

## Backup and recovery

### SQLite

PVCs backing the workspace, gateway, and identity services hold a single
SQLite file each. Two backup strategies:

1. **Volume snapshots** (preferred). Provision a CSI driver that supports
   VolumeSnapshots and run a CronJob to snapshot every 6h.
2. **`sqlite3 .backup`** to an S3 bucket. The `plinth` CLI ships an
   `audit-export` and a `tenant export` command that handle data-level
   backups; combine with periodic snapshots for full DR.

### Postgres

- Enable PIT recovery on RDS / Cloud SQL.
- 7-day retention is the default in the example Terraform module.
- Quarterly: practice a restore into a staging cluster.

### Recovery

1. Stop the affected service: `kubectl -n plinth scale deploy/plinth-workspace --replicas=0`.
2. Restore the volume / DB.
3. Scale back up; auto-migrate runs on the restored database.
4. Verify with `curl -fsS http://plinth-workspace:7421/healthz`.

## Upgrades and rollback

### Upgrades

```bash
helm upgrade plinth ./deploy/helm/plinth \
  --namespace plinth --reuse-values \
  --set global.imageTag=1.1.0
```

Rolling updates use `maxSurge=1, maxUnavailable=0` for stateless services
(gateway, dashboard, MCP servers) and `Recreate` for SQLite-backed
workspace + identity. Schema migrations run on pod startup; if a migration
takes longer than the readiness probe's failure threshold, raise the
`initialDelaySeconds` for that service.

### Rollback

```bash
helm rollback plinth <revision>
```

If a migration was applied that the previous image cannot handle, use
`plinth migrate <svc> --rollback-to <id>` first, then roll back the chart.

## Monitoring

Each service exposes `/metrics` in Prometheus format. Standard metrics:

- `plinth_http_requests_total{service, method, status}`
- `plinth_http_request_duration_seconds{service, method}`
- `plinth_tool_invocations_total{tool_id, tenant_id, cached}`
- `plinth_workflow_steps_total{state}`
- `plinth_load_shed_total`
- `plinth_workers_active`

The pod template annotations `prometheus.io/scrape: "true"` etc. are set on
every Deployment so a stock Prometheus scrape config picks them up. OTLP
is supported via `OTEL_EXPORTER_OTLP_ENDPOINT`. See `docs/observability.md`
for the full attribute list and recommended Grafana dashboards.

Published SLOs (also in `docs/slos.md`):

- Workspace `GET /v1/workspaces/{id}/kv/{key}`: p99 < 50 ms (cached cluster).
- Gateway `POST /v1/invoke` cache-hit: p99 < 30 ms.
- Identity `POST /v1/tokens/verify`: p99 < 20 ms.
- Workflow lease acquisition: p95 < 100 ms.

## Common issues

### "JWT secret missing" on Identity boot

The Helm chart will fail-fast if `identity.jwtSecret` is empty and no
`existingSecret` is set. Provide one via `--set-file` or external secret.

### Pods crash-looping with `unknown column` errors

Migrations are out-of-order for whatever reason. Run `plinth migrate
<svc> --status` to see what's pending and `--apply` to apply them.

### `503 Service Unavailable` on Gateway

Most likely load-shed kicked in (`PLINTH_GATEWAY_LOAD_SHED_ENABLED=true`).
Check `plinth_load_shed_total` and the load on Workspace + Identity.

### Workspace replicas > 1 with SQLite

Won't work — SQLite is single-writer. Either drop to one replica or set
`workspace.postgresUrl`.

### Dashboard 502s through Ingress

Confirm the backend service is up:
`kubectl -n plinth port-forward svc/plinth-dashboard 7424:7424`. If the
port-forward works, the issue is in the Ingress controller / TLS config,
not in Plynf.

### Storage class confusion

If the chart can't find a default storage class, set
`global.storageClass: gp3` (or whatever your CSI driver provides).
Per-service overrides live at `<service>.persistence.storageClass`.

### Migration takes too long during rollout

Bump `initialDelaySeconds` on the readiness probe for the affected service,
and increase the migration timeout if you're using `auto_migrate=false` and
a separate Job. Long-running data migrations should run as Jobs, not as
service-startup tasks.

## See also

- `docs/API_STABILITY.md` — what the v1 surface guarantees.
- `docs/observability.md` — metrics, traces, log attributes.
- `docs/slos.md` — published service-level objectives.
- `PRODUCTION_READINESS.md` — the operator checklist (~50 items).
- `deploy/helm/plinth/README.md` — Helm chart values reference.
- `deploy/terraform/aws-example/README.md` — Terraform starter for AWS.
