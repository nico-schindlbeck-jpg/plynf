# Click-to-Install Gaps — Block 1 Audit

**Purpose**: enumerate every step a non-technical end-user cannot execute today and map each to a Distribution Block that closes it.

**Audit date**: 2026-05-16
**Audited revision**: `main` @ `e82855a` (v1.7.5)
**Reference success path**: §9 of Distribution Roadmap v2 — "PM at Acme Corp goes from `plinth.dev` to running demo in ≤5 minutes without seeing the words Python, Docker, venv, or pip."

---

## 1. Gap Catalogue

### 1.1 PLAYBOOK §1.1 Pre-Flight Checklist — Manual Steps

Every line below is something a customer cannot do without engineer-level skills today.

| # | Manual step (current state) | Auto-isable? | Addressed in | Residual risk |
|---|---|---|---|---|
| 1 | `git status` shows clean tree | n/a (dev-only) | – | – |
| 2 | HEAD is on release commit, README badge matches | n/a (dev-only) | – | – |
| 3 | `git pull` ran successfully | yes | Block 3 (`plinth update`) | Network failure handling |
| 4 | Verify Python 3.11+ present | yes | Block 6 (embedded binary bundles Python) | – |
| 5 | `.venv/` exists, `make install` runs cleanly | yes | Block 6 (no venv needed) | – |
| 6 | `make test` passes — every suite green | n/a (dev-only) | – | – |
| 7 | `make stop`; `lsof -iTCP:7421-7428 -sTCP:LISTEN` is empty | yes | Block 3 (`plinth doctor` checks ports, suggests remediation) | User confusion if port in use by another app |
| 8 | `make serve` starts all 8 services without error | yes | Block 3 (`plinth up`) wraps it | Lifespan order, Docker daemon down |
| 9 | `bash scripts/healthcheck.sh` returns 200 on every endpoint | yes | Block 3 (`plinth doctor`) | – |
| 10 | Dashboard at `:7424` loads — manual visual check | partial | Block 5 (Tauri app auto-opens dashboard window) | First-paint detection |
| 11 | `tail -n 5 /tmp/plinth-logs/*.log` shows no warnings | yes | Block 3 (`plinth logs --since 5m --severity warn`) | – |
| 12 | Run each of 5 demos once, verify exit 0 | partial | Block 4 (Wizard offers "Run sample task") | Demo failure surfacing |
| 13 | Pre-run with target topic to warm caches | n/a (demo-only) | – | – |
| 14 | Terminal font 16-18pt, browser zoom 110-125%, all tabs closed | n/a (presenter-only) | – | – |
| 15 | Manual recovery shell command if a service dies mid-call | yes | Block 5 (tray menu "Restart") + Block 3 (`plinth restart <svc>`) | – |

**Net**: 11 of 15 steps are auto-isable. Steps 1, 2, 6 are dev-only (no end-user impact). Step 13, 14 are presenter ergonomics.

---

### 1.2 docker-compose.yml — Build-vs-Pull Audit

```bash
$ grep -c "build:" docker-compose.yml        # → 13
$ grep -c "^    image:" docker-compose.yml   # → 13
```

Every service has BOTH a `build:` directive AND an `image:` tag. The `image:` references are local-only (e.g. `plinth/workspace:0.1.0`) — they are NOT pulled from a registry.

**Consequence today**: end-user must clone the repo and run `docker compose up --build` (5-15 min cold build per service).

**What Block 2 changes**: produce `deploy/compose.prod.yml` where every `image:` resolves to `ghcr.io/<org>/plinth-<svc>:${PLINTH_VERSION}` with the `build:` block removed. User runs `docker compose -f deploy/compose.prod.yml pull && up -d` (30s - 5 min depending on bandwidth, no local build).

**Service-by-service Dockerfile status**:

| Service | Dockerfile present? | Notes |
|---|---|---|
| `services/workspace/` | ✅ | python:3.11 base, ~250 MB |
| `services/gateway/` | ✅ | same |
| `services/identity/` | ✅ | same |
| `services/dashboard/` | ✅ | bundles vanilla SPA |
| `mock-mcp-server/` | ✅ | – |
| `mcp-servers/github/` | ✅ | – |
| `mcp-servers/slack/` | ✅ | – |
| `mcp-servers/linear/` | ✅ | – |
| `mcp-servers/notion/` | ✅ | – |
| `mcp-servers/google-workspace/` | ✅ | – |
| `mcp-servers/atlassian/` | ✅ | – |
| `mcp-servers/salesforce/` | ✅ | – |
| `mcp-servers/asana/` | ✅ | – |
| **Total** | **13/13** | |

**Block 2 prerequisite**: slim-base images. Current `python:3.11` images are ~250 MB each → 3.25 GB across 13 services. Refactor to `python:3.11-alpine` multi-stage → ≤80 MB each → 1 GB total. Shared base layer reduces effective pull further.

---

### 1.3 `install/install.sh` — Existence and Function

**File exists**: ✅ `install/install.sh` (16,711 bytes, 509 lines)
**Marketing claim**: `curl -fsSL https://plinth.dev/install.sh | sh` on the landing page
**Functional?**: ⚠️ partially — file works, but the URL doesn't because `plinth.dev` is unregistered.

**What `install/install.sh` actually does**:
- Detects OS (`uname -s`, `uname -m`)
- Installs `git`, `python3.11` via Homebrew (macOS) or apt (Linux) if missing
- Clones repo to `${PLINTH_HOME:-$HOME/.plinth}`
- Runs `python -m venv` and `pip install -e` × 14 services (yes, fourteen — `install/run_all.py:60-66` and Makefile target invocations)
- Installs launchd (macOS) / systemd-user (Linux) units for auto-start
- Opens dashboard at `http://localhost:7424` via `open` / `xdg-open`

**Critical observations**:
- Installer requires Python 3.11+ to be installable; if user has only system Python 3.9 (macOS default), installer **fails or installs Python via Homebrew** (~5-10 min adds to install time)
- ~14 pip installs in sequence, each downloading ~5-15 MB of deps → 5-10 min total on average broadband
- No SHA256 verification of the script itself (TOFU vulnerability)
- No `--version` pinning support (always pulls `main`)
- Idempotent on second run ✅
- Has comprehensive flags: `--verbose`, `--skip-autostart`, `--skip-services`, `--skip-open`, `--no-update`, `--dry-run`, `--help`

**Block 3 supersedes this**: the new Go `plinth` CLI ships as a single static binary, no Python required, downloads pre-built containers OR the embedded binary. `install/install.sh` becomes a compatibility shim that just downloads + execs `plinth`.

---

### 1.4 OAuth Provider Matrix

Today each MCP requires environment variables with the user's own OAuth Client-ID and Client-Secret. Provider-by-provider audit:

| Provider | Env var pattern | PKCE-fähig? | Confidential-Secret required? | Distribution-friendly? |
|---|---|---|---|---|
| GitHub | `PLINTH_OAUTH_GITHUB_{CLIENT_ID,CLIENT_SECRET}` | ✅ | No (PKCE-only OK) | ✅ Anyone can create app instantly |
| Linear | `PLINTH_OAUTH_LINEAR_{CLIENT_ID,CLIENT_SECRET}` | ✅ | No | ✅ Open program |
| Notion | `PLINTH_OAUTH_NOTION_{CLIENT_ID,CLIENT_SECRET}` | ✅ | Yes for confidential clients | ⚠️ Public Integration review for distribution |
| Slack | `PLINTH_OAUTH_SLACK_{CLIENT_ID,CLIENT_SECRET}` | ❌ (uses OAuth 2 not 2.1) | Yes | ❌ App Directory review (4-8 weeks) for distribution |
| Google Workspace | `PLINTH_OAUTH_GOOGLE_{CLIENT_ID,CLIENT_SECRET}` | ✅ | Yes | ❌ CASA Assessment for sensitive scopes (€10k+, 2-4 months) |
| Atlassian | `PLINTH_OAUTH_ATLASSIAN_{CLIENT_ID,CLIENT_SECRET}` | ✅ | Yes | ❌ Marketplace listing (2-3 weeks) for distribution |
| Salesforce | `PLINTH_OAUTH_SALESFORCE_{CLIENT_ID,CLIENT_SECRET}` | ✅ | Yes | ❌ AppExchange Security Review (3-6 months) |
| Asana | `PLINTH_OAUTH_ASANA_{CLIENT_ID,CLIENT_SECRET}` | ✅ | No | ⚠️ App Gallery (1-2 weeks) |

**Today's reality**: every end-user has to register their own OAuth app with each provider before they can use that MCP. That is a 30-60 minute click-through per provider. Eight providers = 4-8 hours of paperwork before the agent can run.

**Block 7a (v1.6)**: PKCE-broker on `oauth.plinth.dev` for **GitHub, Linear, Notion** (no client-secret needed; broker holds pre-registered Client-IDs which are public per OAuth spec).

**Block 7b (v1.7, parallel approval-lanes)**: Slack, Atlassian, Asana — submit apps for distribution review now, ship when approved.

**Block 7c (v2.0, compliance workstream)**: Google Workspace, Salesforce — months of Security Review work, requires legal entity + ~€15k assessment budget.

**Power-user escape hatch in all blocks**: `PLINTH_OAUTH_BROKER_DISABLED=true` falls back to today's per-provider env vars.

---

### 1.5 Postgres Setup — Documentation Existence

**Audit**: searched docs/ for "postgres" mentions.

| Doc | Postgres coverage | End-user friendly? |
|---|---|---|
| `docs/multi-region.md` | mentions deployment topology | ❌ assumes Postgres exists |
| `docs/deployment.md` | mentions `PLINTH_STORAGE_DRIVER=postgres` env var | ⚠️ one paragraph, no setup steps |
| `docs/adr/0002-storage-postgres-and-objectstore.md` | architectural decision | ❌ not setup |
| `docs/adr/0006-multitenancy-model.md` | references Postgres-backed tenancy | ❌ not setup |

**Net**: there is no end-to-end "how to set up Postgres for Plinth" guide. SQLite is the default, so for Free tier / hobby use, no Postgres needed. For Pro/Enterprise, Postgres requires:
- Provision Postgres ≥14 with `pgvector` extension
- Create databases (`plinth_workspace`, `plinth_gateway`, `plinth_identity`)
- Set 14 env vars (`PLINTH_WORKSPACE_DB_URL` etc.)
- Run `make migrate-all` (or equivalent)

**Block 8 deliverable**: `docs/deploy/postgres.md` with copy-paste-able steps. Estimated 0.25 PT.

**Note**: `PLINTH_STORAGE_DRIVER=postgres` is already implemented in code — this is purely a docs gap.

---

### 1.6 `PLINTH_DATA_DIR` Default — Persistence Risk

```bash
$ grep -rn "PLINTH_DATA_DIR" Makefile install/run_all.py docker-compose.yml
Makefile:14:PLINTH_DATA_DIR ?= /tmp/plinth-data
install/run_all.py:35:DATA_DIR = Path(os.environ.get("PLINTH_DATA_DIR", str(PLINTH_HOME / "state" / "data")))
docker-compose.yml:24:      PLINTH_DATA_DIR: /data
docker-compose.yml:47:      PLINTH_DATA_DIR: /data
```

**Three different defaults across three install paths**:

| Path | Default | Risk |
|---|---|---|
| `make serve` (dev) | `/tmp/plinth-data` | macOS `purge` and reboots wipe `/tmp`; Linux often mounts `/tmp` as tmpfs → **data loss** |
| `install/install.sh` (end-user) | `$HOME/.plinth/state/data` | ✅ safe |
| `docker compose up` | `/data` (volume) | ✅ safe — Docker volume persists |

**Severity**: **medium for end-users, high for devs.** End-users via installer or Docker are fine. Devs lose their experimental data on every reboot — affects credibility when a stakeholder says "let me try it" and runs `make serve` instead of the installer.

**Block 1.5 fix**: change Makefile default to `platformdirs.user_data_dir("plinth")` (resolves to `~/Library/Application Support/Plinth` on macOS, `~/.local/share/plinth` on Linux, `%APPDATA%\Plinth` on Windows). Add migration helper that copies `/tmp/plinth-data` to new default if it exists and non-empty.

---

### 1.7 SDK Distribution — Registry Status

**Marketing claim**: "5 SDKs (Python, TS, Go, Swift, Kotlin)" with `pip install plinth-sdk`, `npm i @plinth/sdk`, etc.

**Audit**: queried public registries for the package names.

| Registry | Package name expected | Status |
|---|---|---|
| PyPI | `plinth` or `plinth-sdk` | ❌ Not published |
| npm | `@plinth/sdk` or `plinth` | ❌ Not published |
| Go module proxy | `github.com/plinth/sdk-go` | ❌ No tagged release |
| Swift Package Index | `github.com/plinth/sdk-swift` | ❌ Not indexed |
| Maven Central | `dev.plinth:sdk` | ❌ Not published |

**Reality today**: developers can use the SDK only by cloning the repo and running `pip install -e ./sdk/python`. The "five SDKs published" promise on the landing is aspirational.

**Block 8 sub-deliverable** (new — wasn't in v2 plan):
- `release-python-sdk.yml` — trusted publishing to PyPI on tag
- `release-ts-sdk.yml` — npm publish with provenance attestation
- `release-go-sdk.yml` — go mod tag automation (Go modules are pull-based, just need a clean tag)
- Swift Package Index — register the repo once (no recurring workflow)
- Maven — defer to v1.7 (most complex of the lot, needs OSSRH account + GPG signing)

Estimated effort: ~1 PT total, can run in parallel with Block 2.

---

### 1.8 Subdomain Inventory

| Subdomain | Promised in | Status | Replaceable? |
|---|---|---|---|
| `plinth.dev` (apex) | hero, install URL | ❌ unregistered | – |
| `docs.plinth.dev` | nav, footer, MCP-tab | ❌ doesn't exist | Use `/docs` on apex if rushed |
| `blog.plinth.dev` | footer | ❌ doesn't exist | Skip until cloud product exists |
| `status.plinth.dev` | footer, terms SLA | ❌ doesn't exist | Better Stack free tier @ `status.plinth.dev` after DNS |
| `oauth.plinth.dev` | Block 7 architecture | ❌ doesn't exist | Cloudflare Workers / Fly.io deploy |
| `telemetry.plinth.dev` | Block 8 architecture | ❌ doesn't exist | Cloudflare Workers, opt-in only |
| `signup.plinth.dev` or `app.plinth.dev` | pricing CTAs | ❌ doesn't exist | Cloud product not built — placeholder fine |

**Block 0.3 prerequisite**: register `plinth.dev` (must be the apex). All subdomains are DNS records under it.

---

### 1.9 Mailbox Inventory

| Address | Linked from | Status |
|---|---|---|
| `hello@plinth.dev` | pricing custom-CTA, imprint, /security | ❌ undeliverable |
| `sales@plinth.dev` | pricing Enterprise CTA | ❌ undeliverable |
| `legal@plinth.dev` | terms, imprint | ❌ undeliverable |
| `privacy@plinth.dev` | privacy policy | ❌ undeliverable |
| `security@plinth.dev` | security.txt, security page | ❌ undeliverable |
| `press@plinth.dev` | imprint | ❌ undeliverable |
| `noreply@plinth.dev` | system-generated mails (future) | ❌ undeliverable |

**Action**: mailbox provider must be set up before any Pro/Enterprise inbound is possible. Fastmail Standard (€5/mo) handles all 7 aliases under one inbox. Forwarding-only at €0 also works (Cloudflare Email Routing).

---

## 2. Summary Matrix

| Gap area | Severity | Blocker for | Block(s) that close it |
|---|---|---|---|
| Domain `plinth.dev` unregistered | 🔴 Critical | Everything | User action (not a Block) |
| Mailboxes don't exist | 🔴 Critical | Tobias/Sarah persona | User action |
| `docker-compose.yml` build-only | 🟡 High | "Pull, don't build" UX | Block 2 |
| SDK packages not published | 🟡 High | Developer adoption | Block 8 sub-deliverable |
| OAuth requires per-user app registration | 🟡 High | Anna persona | Block 7a (v1.6 for 3 providers) |
| `PLINTH_DATA_DIR=/tmp/...` Makefile default | 🟡 High | Dev credibility | Block 1.5 |
| 14× pip install on cold start | 🟢 Medium | Cold-start ≤5 min SLA | Block 6 (embedded eliminates pip entirely) |
| Postgres setup undocumented | 🟢 Medium | Pro/Enterprise self-host | Block 8 sub-deliverable |
| Subdomains missing | 🟢 Medium | Various marketing claims | User action + Block 8 |
| 5 demos manual to run | 🟢 Medium | Wizard "Run sample task" | Block 4 |
| Tauri desktop bundle missing | 🟢 Medium | Click-to-install promise | Block 5 |
| First-run wizard missing | 🟢 Medium | ≤3-click setup promise | Block 4 |
| `plinth doctor` CLI missing | 🟢 Medium | Self-service troubleshooting | Block 3 |
| Cosign-signed images missing | 🟢 Medium | Enterprise trust | Block 2 |

---

## 3. What Block 1 Does NOT Cover

Out of scope for this audit, called out so we don't pretend they're solved:

1. **Threat model documentation** — `docs/threat-model.md` does not exist today, even though `/security` page references it. Separate workstream.
2. **DPA / SOC 2 / ISO 27001 artefacts** — none exist. Multi-month compliance workstream, not engineering.
3. **Legal entity** — `Plinth GmbH` referenced in imprint is a placeholder. Real entity formation is a founder action, not engineering.
4. **Cloud product itself** — Stripe + Auth + dashboard + multi-tenant routing. Block 9+ work, separate roadmap.

---

## 4. Recommended Block Sequence Adjustments

Based on this audit, two adjustments to the v2 roadmap:

1. **Add Block 8 sub-deliverables for SDK publishing** — was implicit in v2, now explicit. Adds ~1 PT to Block 8, total Block 8 now 3 PT.
2. **Block 1.5 (PLINTH_DATA_DIR fix)** can include adding a migration helper that copies `/tmp/plinth-data` to the new default. This protects dev workflows from data loss on first run after upgrade. Adds 0.25 PT, total Block 1.5 now 0.75 PT.

No other v2 estimates change as a result of this audit.

---

## 5. Verification Commands

To re-run this audit at any time:

```bash
# Manual step count
grep -c '^- \[' PLAYBOOK.md

# Compose build vs pull
grep -c 'build:' docker-compose.yml
grep -c '^    image:' docker-compose.yml

# Service Dockerfiles
find services/ mcp-servers/ mock-mcp-server/ -name "Dockerfile" -type f | wc -l   # expect 13

# OAuth provider env vars
grep -rh "PLINTH_OAUTH_.*_CLIENT" services/ mcp-servers/ | sort -u

# Data dir defaults
grep -rn "PLINTH_DATA_DIR" Makefile install/ docker-compose.yml

# SDK publishing status
curl -s https://pypi.org/pypi/plinth/json | jq -r '.info.version // "not published"'
npm view @plinth/sdk version 2>/dev/null || echo "not published"
```

---

## 6. Estimated Effort to Close All Gaps

| Block | Effort (PT) | Closes Gap # |
|---|---|---|
| User actions (domain, mailboxes, Apple Dev) | n/a (you, async) | 1.8, 1.9, Block 5 prereq |
| Block 1.5 | 0.75 | 1.6 |
| Block 2 | 3 | 1.2 |
| Block 3 | 4.5 | 1.1 (most), 1.3 |
| Block 4 | 3.5 | 1.1 (steps 10, 12) |
| Block 5 | 7 | 1.1 (step 15), wizard launcher |
| Block 6 | 6 | 1.1 (steps 4, 5, 8), eliminates pip cost |
| Block 7a | 2.5 | 1.4 (3 providers) |
| Block 8 + SDK | 3 | 1.5, 1.7, 1.8 (docs subdomain) |
| **Total Eng** | **30.25** | – |

This number replaces the v2 plan's 38-40 PT estimate. Reduction comes from (a) `image:` directives already present so Block 2 is smaller than feared, (b) Embedded Mode benefitting from ADR 0009's Candidate D (no service-side refactor needed, Block 6 is 6 PT not 6.5).

---

## 7. Sign-off

This audit is closed when:
- [x] Every step in PLAYBOOK §1.1 has been categorised
- [x] Every Dockerfile in the repo has been inventoried
- [x] Every OAuth provider's distribution status has been researched
- [x] Every subdomain and mailbox claim on the website has been verified
- [x] A summary matrix maps each gap to a Block

**Status**: ✅ Complete. Ready to feed into Block 1.5+ execution.
