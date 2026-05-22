# Multi-Region — Operator Playbook (v1.0)

> **Status**: Plynf v1.0 ships **multi-region scaffolding**, not a turnkey
> multi-region deployment. The configuration knobs, replication primitives,
> and SDK region-awareness are all in place; the cross-region orchestration
> (the cron job / k8s sidecar / agent that actually streams writes between
> regions) is intentionally left to the operator.

## Why scaffolding only

Plynf's storage primitives (SQLite by default, Postgres in production) are
deployed close to the workspace + identity services for latency reasons.
A "real" multi-region deploy means making three orthogonal choices:

1. **Topology** — primary/replica? multi-primary with conflict resolution?
   federated tenants pinned to one region each?
2. **Storage substrate** — log-shipping for SQLite, streaming replication
   for Postgres, managed services like Aurora Global, or external CRDTs?
3. **Routing** — geo-DNS? client-side region pinning? sticky sessions?

These are operator decisions. The v1.0 surface gives you the hooks; the
choices are yours.

## Three modes

Every Plynf service accepts `PLINTH_REPLICATION_MODE`:

| Mode         | Behaviour                                                    | When to use                  |
| ------------ | ------------------------------------------------------------ | ---------------------------- |
| `standalone` | Single-region (default). No replication. Identical to v0.6.  | Default — most deployments.  |
| `primary`    | Accepts writes; appends every mutation to `replication_log`. | The authoritative region.    |
| `replica`    | Read-only. Mutating verbs return `421 REPLICA_READ_ONLY` with `X-Plynf-Primary-Region` + `X-Plynf-Primary-URL`. | Geo-distributed read mirrors. |

`standalone` is the v0.6 behaviour exactly — your existing single-region
deployment doesn't see any change unless you opt in.

## Region configuration

Each service reads four env vars:

```bash
PLINTH_REGION_ID=eu-west-1                    # this instance's region
PLINTH_REGION_PEERS=us-east-1,ap-south-1     # comma-separated peer ids
PLINTH_REGION_PEER_US_EAST_1_URL=https://us.plinth.example
PLINTH_REGION_PEER_AP_SOUTH_1_URL=https://ap.plinth.example
PLINTH_REPLICATION_MODE=primary               # or replica / standalone
PLINTH_REGION_PRIMARY_URL=https://eu.plinth.example  # replicas only
```

Underscores in the env var ID translate back to dashes for the peer id
(shells can't carry dashes in variable names). So
`PLINTH_REGION_PEER_US_EAST_1_URL` populates `region_peer_urls["us-east-1"]`.

## Replication log

When `replication_mode=primary`, every successful mutating request
appends a row to `replication_log` (`workspace.db`):

```sql
CREATE TABLE replication_log (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  workspace_id TEXT,
  payload TEXT NOT NULL,
  occurred_at TIMESTAMP NOT NULL,
  region_id TEXT NOT NULL
);
```

Three admin endpoints expose it:

```
GET  /v1/admin/replication/log?since=<seq>&limit=<int>  → list[Entry]
GET  /v1/admin/replication/status                       → mode, current_seq, peers_lag
POST /v1/admin/replication/apply  body: list[Entry]     → applied, skipped
```

A replica pulls from `/log` on its primary, then POSTs into `/apply`
locally. The apply endpoint is **idempotent** by `seq`: re-pulling the
same range is safe.

For Postgres deployments, **don't** use the replication log — use the
managed service's native streaming replication (RDS, Cloud SQL, Aurora
Global). The replication log is a SQLite-friendly fallback; it's not
optimized for the throughput Postgres deployments target.

## Operator-side responsibilities

You wire the actual replication. Common patterns:

1. **Cron pull** — every 60s, the replica runs:
   ```bash
   CURRENT=$(curl -s $REPLICA/v1/admin/replication/status | jq .current_seq)
   curl $PRIMARY/v1/admin/replication/log?since=$CURRENT \
     | jq .entries \
     | curl -X POST -H "Content-Type: application/json" \
            --data @- $REPLICA/v1/admin/replication/apply
   ```
2. **k8s sidecar** — same shape, packaged as a controller; reconciles
   `status.next_since` against the primary.
3. **Plynf agent** — write a tiny agent that uses the SDK and
   `client.gateway.invoke("plinth.replication.pull", ...)` if you want
   the audit trail / cost tracking that comes with the gateway.

In all cases, the apply endpoint dedupes by `seq`, so retries are safe.

## Failure modes

- **Primary down**: replicas keep serving GETs. Writes still 421 with
  `X-Plynf-Primary-Region` + `X-Plynf-Primary-URL`; the SDK sees those
  headers and routes to the next fallback region. Eventually consistent
  for reads, hard-fail for writes — by design.
- **Replica behind**: `peers_lag` in `/status` shows the gap. Operators
  alert on this. Bad replicas are de-pooled at the load balancer.
- **Split brain**: two regions both promoted to primary. The replication
  log doesn't prevent this — it's the operator's job. Use external
  coordination (Consul, etcd, manual failover) and have a runbook.

## Migration path

1. **standalone → primary**: flip `PLINTH_REPLICATION_MODE=primary`
   on the existing single-region deployment. The `replication_log`
   table is created on next boot; nothing else changes.
2. **primary → multi-replica**: spin up a second region with
   `replication_mode=replica` and the primary's URL in
   `region_peer_urls`. Wire the cron/sidecar puller. SDK clients
   automatically fail over via `X-Plynf-Primary-Region`.
3. **multi-replica → multi-primary** (out of scope for v1.0):
   you're now in conflict-resolution territory. Plynf doesn't
   ship a CRDT layer; use Postgres + Aurora Global / Spanner, or
   shard tenants across regions.

## SDK region-awareness

Both Python and TypeScript SDKs accept region + fallback parameters:

```python
# Python
client = Plynf(
    workspace_url="https://workspace.eu-west.plinth.example",
    gateway_url="https://gateway.eu-west.plinth.example",
    region="eu-west-1",
    fallback_regions=["us-east-1"],
    fallback_workspace_urls={
        "us-east-1": "https://workspace.us-east.plinth.example",
    },
    fallback_gateway_urls={
        "us-east-1": "https://gateway.us-east.plinth.example",
    },
)
```

```ts
// TypeScript — same surface, camelCase
const client = new Plynf({
    workspaceUrl: "https://workspace.eu-west.plinth.example",
    gatewayUrl: "https://gateway.eu-west.plinth.example",
    region: "eu-west-1",
    fallbackRegions: ["us-east-1"],
    fallbackWorkspaceUrls: {
        "us-east-1": "https://workspace.us-east.plinth.example",
    },
    fallbackGatewayUrls: {
        "us-east-1": "https://gateway.us-east.plinth.example",
    },
});
```

The SDK retries fallbacks in declared order on:
- connection errors
- 503 / 502 / 504 responses
- 421 (Misdirected Request) + `X-Plynf-Primary-Region` /
  `X-Plynf-Primary-URL` (read-replica redirect; 409 is also accepted
  for backwards compatibility with pre-spec deployments)

A warning is logged on every failover. The redirect retry is bounded:
each unique base URL is tried at most once per request, so a misconfigured
pair of replicas can never produce an infinite loop. 4xx errors other than
421 are NOT retried; they surface to the caller unchanged.

When the response carries `X-Plynf-Primary-URL`, the SDK trusts it
**only** if its origin matches a configured candidate URL (the primary
or a `fallback_*_urls` entry). A hostile replica can't redirect the SDK
at an attacker-controlled host.

## Discovery endpoint

Every service exposes `GET /v1/regions`:

```json
{
  "current": "eu-west-1",
  "mode": "primary",
  "peers": [
    {
      "id": "us-east-1",
      "url": "https://us.plinth.example",
      "status": "up",
      "lag_ms": 47.3,
      "last_seen_at": "2026-05-08T12:00:00+00:00"
    }
  ]
}
```

`status` is one of `up | degraded | down`, derived from a `GET /healthz`
probe with a 30-second cache (configurable via
`PLINTH_REGIONS_STATUS_CACHE_TTL_SECONDS`). `lag_ms` reflects the
round-trip time — operators correlate this against their replication
lag dashboards.

## Verification commands

After flipping the env vars, sanity-check each instance with curl:

```bash
# Primary
curl -s https://eu.plinth.example/v1/regions | jq .
# {
#   "current": "eu-west-1",
#   "mode": "primary",
#   "peers": [ { "id": "us-east-1", "url": "...", "status": "up", "lag_ms": 47.3 } ]
# }

# Replica reports its mode
curl -s https://us.plinth.example/v1/regions | jq .mode
# "replica"

# A write to the replica returns 421 with both redirect headers
curl -i -X POST https://us.plinth.example/v1/workspaces -d '{"name":"x"}'
# HTTP/2 421
# X-Plynf-Primary-Region: eu-west-1
# X-Plynf-Primary-URL: https://eu.plinth.example
# Location: https://eu.plinth.example/v1/workspaces
```

Reads work fine on a replica; only mutating verbs trigger the redirect.

## Postgres streaming replication recipe

For Postgres deployments, ignore the SQLite replication log entirely —
use Postgres's native streaming replication, which is faster and
battle-tested:

```bash
# Primary postgresql.conf
wal_level = replica
max_wal_senders = 5
wal_keep_size = 1GB
hot_standby = on

# Replica
primary_conninfo = 'host=primary.db.internal user=replicator password=...'
```

Then set `PLINTH_DATABASE_URL` on the replica to the local Postgres
read endpoint, and `PLINTH_REPLICATION_MODE=replica` on the Plynf
service. Plynf's middleware redirects writes; Postgres handles the
data. Managed services (RDS, Cloud SQL, Aurora Global) automate the
streaming setup — same Plynf config either way.

## SQLite log-shipping recipe

SQLite deployments use the per-region replication log. Two patterns:

**Cron + rsync** — simplest, lag of 1× the cron interval:
```bash
# Pull the latest entries from the primary's log API every 60s,
# POST them into the replica's apply endpoint. Idempotent by ``seq``.
* * * * * \
  CURRENT=$(curl -s https://us.plinth.example/v1/admin/replication/status \
              | jq -r .current_seq); \
  curl -s "https://eu.plinth.example/v1/admin/replication/log?since=$CURRENT" \
    | jq .entries \
    | curl -s -X POST -H "Content-Type: application/json" \
        --data @- https://us.plinth.example/v1/admin/replication/apply
```

**WAL file shipping** — when the replica also needs blob/file content:
```bash
# Periodic rsync of data_dir, paired with the apply endpoint above
# for the structured KV / channel mutations.
*/5 * * * * rsync -az --delete \
  primary.internal:/var/lib/plinth/blobs/ \
  /var/lib/plinth/blobs/
```

The apply endpoint dedupes by `seq`, so retries and overlapping pulls
are safe. Both recipes converge eventually; choose based on your RPO
target.

## Failover playbook (replica → primary)

When the primary region fails and you need to promote a replica:

1. Stop replication pulls on every replica (kill the cron / sidecar).
2. Pick the replica with the highest `current_seq` from
   `/v1/admin/replication/status`.
3. On that instance, set `PLINTH_REPLICATION_MODE=primary` and
   `PLINTH_REGION_PRIMARY_URL=""` (clear the old primary pointer).
4. Restart the service. The next write lands locally and starts
   appending to its own replication log.
5. Update the other replicas' `PLINTH_REGION_PRIMARY_URL` to point
   at the new primary, restart their pullers.
6. Update SDK clients' `region` and `fallback_regions` configs (or
   trust the SDK's `X-Plynf-Primary-URL` redirect during the
   transition window).
7. When the dead region comes back, bring it up as `replica` first;
   only re-promote after a manual cutover. **Never** boot two primaries
   simultaneously — Plynf doesn't ship conflict resolution.

## Replication latency expectations

| Substrate | Pattern              | Typical lag    |
| --------- | -------------------- | -------------- |
| SQLite    | Cron pull (60s)      | 30–90 seconds  |
| SQLite    | Cron pull (5s)       | 3–10 seconds   |
| Postgres  | Streaming repl.      | 50–500 ms      |
| Postgres  | Aurora Global        | <1 second      |

The SDK doesn't wait for replication; reads against a replica may see
slightly stale data. If your application can't tolerate that, route
reads to the primary and use replicas only for failover.

## What's NOT in v1.0

- **The cross-region orchestrator** — there's no built-in cron/sidecar
  agent. Build your own (it's ~30 lines of curl).
- **Multi-primary writes** — only one region can be `primary` at a time.
  Promotion is a manual operator action.
- **Geo-DNS routing** — Plynf doesn't manage the DNS layer; that's
  your load balancer / Cloudflare / Route53 territory.
- **Conflict resolution for concurrent writes** — there isn't any.
  v1.0 is single-primary-eventual-replica.

## Backwards compatibility

`standalone` is the default. Existing v0.6 deployments see no behaviour
change — the `replication_log` table is created (empty) but never
written to, and the new endpoints are additive.
