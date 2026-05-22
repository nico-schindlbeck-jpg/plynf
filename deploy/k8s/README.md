# Plynf — Kubernetes manifests

Plain `kubectl apply -k` overlay-friendly manifests for every Plynf service.
Use this when you want fine-grained control without the abstractions of the
Helm chart (which lives one directory over at `../helm/plinth/`).

## Layout

```
deploy/k8s/
├── namespace.yaml          # plinth namespace (PSA: restricted)
├── workspace.yaml          # ConfigMap + PVC + Deployment + Service
├── gateway.yaml
├── identity.yaml
├── dashboard.yaml
├── mock-mcp.yaml
├── github-mcp.yaml
├── slack-mcp.yaml
├── linear-mcp.yaml
├── postgres.yaml           # Optional in-cluster Postgres (StatefulSet)
├── ingress.yaml            # Optional nginx Ingress for dashboard + gateway
├── secrets.example.yaml    # TEMPLATE — never apply unmodified
├── kustomization.yaml      # base
└── overlays/
    ├── dev/                # 1-replica everything, debug logging
    └── prod/               # HPA, PDB, larger resources
```

## Quick start

1. Generate the real Secret out-of-band — sealed-secrets, External Secrets
   Operator (ESO), AWS Secrets Manager via the Secrets Store CSI driver, etc.
   The schema is in `secrets.example.yaml`. **Do not apply that template
   verbatim.**

2. Apply the base manifests:

   ```bash
   kubectl apply -k deploy/k8s/
   ```

   That creates the `plinth` namespace, all eight services, and the matching
   ConfigMaps + Services. The `mock-mcp`, `github-mcp`, `slack-mcp`, and
   `linear-mcp` deployments come up too — disable any you don't want by
   editing `kustomization.yaml`.

3. Sanity-check:

   ```bash
   kubectl -n plinth rollout status deploy/plinth-workspace
   kubectl -n plinth rollout status deploy/plinth-gateway
   kubectl -n plinth rollout status deploy/plinth-identity
   kubectl -n plinth get pods,svc
   ```

4. Port-forward the dashboard for a smoke test:

   ```bash
   kubectl -n plinth port-forward svc/plinth-dashboard 7424:7424
   open http://localhost:7424
   ```

## Production overlay

```bash
kubectl apply -k deploy/k8s/overlays/prod/
```

The prod overlay:

- Sets `plinth-gateway` and `plinth-dashboard` to 3 replicas.
- Adds a `HorizontalPodAutoscaler` for `plinth-gateway` (3..10 pods, CPU 70%).
- Adds a `PodDisruptionBudget` (`minAvailable: 2`).
- Bumps requests/limits on Gateway, Workspace, and Identity.
- Bumps the workspace PVC to 100 GiB.

`plinth-workspace` and `plinth-identity` stay at 1 replica because the SQLite
backing store is single-writer. Scale them to >1 only after pointing
`PLINTH_POSTGRES_URL` at a managed Postgres (set the `postgres-url` key in
the `plinth-secrets` Secret).

## Image registry

Manifests reference `ghcr.io/nico-schindlbeck-jpg/plinth/<service>:1.0.0`.
Override per-image via the `images:` block in `kustomization.yaml`, or run a
patch:

```bash
kubectl apply -k deploy/k8s/ \
  --kustomize-config <(cat <<'EOF'
images:
  - name: ghcr.io/nico-schindlbeck-jpg/plinth/workspace
    newName: registry.internal/plinth/workspace
    newTag: 1.0.1
EOF
)
```

## Ingress

`ingress.yaml` is intentionally **not** part of the base kustomization. Apply
it once you have an Ingress controller (nginx assumed) and DNS configured:

```bash
kubectl apply -f deploy/k8s/ingress.yaml
```

Edit hostnames + the cert-manager annotation first.

## Secrets

The required keys (see `secrets.example.yaml`):

| Key | Required | Used by |
|-----|----------|---------|
| `jwt-secret` | yes | identity |
| `oauth-encryption-key` | only if Gateway OAuth | gateway |
| `github-oauth-client-id` / `-secret` | only if GitHub MCP OAuth | gateway |
| `slack-oauth-client-id` / `-secret` | only if Slack MCP OAuth | gateway |
| `linear-oauth-client-id` / `-secret` | only if Linear MCP OAuth | gateway |
| `postgres-url` | only if Postgres-backed | workspace, gateway, identity |

Generate a strong JWT secret:

```bash
openssl rand -base64 48 | tr -d '\n'
```

## See also

- `../helm/plinth/` — the Helm chart, which is what most operators want.
- `../terraform/aws-example/` — an example Terraform module that provisions
  EKS + RDS + S3 and deploys the Helm chart on top.
- `docs/deployment.md` — full operator handbook, including upgrade and
  rollback procedures.
- `docs/PRODUCTION_READINESS.md` — pre-launch checklist.
