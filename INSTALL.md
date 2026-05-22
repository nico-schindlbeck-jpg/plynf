# Install Plynf

Three install paths, sorted by how much you want Plynf to do for itself:

1. **One-liner installer** — fastest, recommended for individual developers
2. **Docker Compose** — full 13-service stack, recommended for teams
3. **From source** — when you want to modify the runtime itself

If you only have five minutes, do path 1. The rest can come later.

---

## Path 1. One-liner installer

A single `curl` pipe sets up everything: clones the repo into `~/.plynf`, installs Python dependencies into an isolated venv, registers auto-start (launchd on macOS, systemd-user on Linux), opens the dashboard.

```bash
curl -fsSL https://plynf.com/install.sh | sh
```

Default install location: `~/.plynf`. Override with `PLYNF_HOME=/some/path`. See `install/install.sh --help` for all flags (verbose mode, dry-run, skipping services, etc.).

When the installer is done:

- Dashboard is open in your default browser at `http://localhost:7420`
- The `plynf` command is on your PATH (`~/.local/bin/plynf`)
- Services auto-start on next login
- Run `plynf doctor` if anything looks wrong

**What this path is good for**: trying Plynf solo, running it on a single laptop, evaluating before committing to anything heavier.

**What it is not for**: production. The installer assumes single-user, no rotation of secrets, no high-availability. Use path 2 for that.

---

## Path 2. Docker Compose (production)

Production-grade, multi-service, signed images pulled from GitHub Container Registry. All 13 services as separately-restartable containers.

### Quick start

```bash
# Pin to a known release. See https://github.com/nico-schindlbeck-jpg/plynf/releases
export PLYNF_VERSION=v1.7
# (PLYNF_ORG defaults to nico-schindlbeck-jpg until the dedicated org exists)

# Clone (or download just the compose file from a release)
git clone --depth 1 --branch "$PLYNF_VERSION" https://github.com/nico-schindlbeck-jpg/plynf
cd plynf

# Pull pre-built images and start
docker compose -f deploy/compose.prod.yml pull
docker compose -f deploy/compose.prod.yml up -d --wait
```

Expect 30-90 seconds for first-time pull (depends on bandwidth — total ~1.2 GB across 13 multi-arch images). After that, restarts are seconds.

### Verifying image signatures (recommended)

Every image is cosign-signed via keyless OIDC. Before running in production:

```bash
# Install cosign once
brew install cosign           # macOS
# OR
go install github.com/sigstore/cosign/v2/cmd/cosign@latest

# Verify a specific image
cosign verify \
  "ghcr.io/nico-schindlbeck-jpg/plynf-workspace:v1.7" \
  --certificate-oidc-issuer "https://token.actions.githubusercontent.com" \
  --certificate-identity-regexp "^https://github.com/nico-schindlbeck-jpg/.+"
```

A successful output ends with the image's manifest digest. Anything else means the image was tampered with — don't run it.

### Pinning by digest

For supply-chain-conscious deployments, switch from mutable tags to content-addressable digests:

```bash
scripts/pin-compose-digests.sh deploy/compose.prod.yml v1.7
# → writes deploy/compose.prod.pinned.yml with @sha256:... refs

docker compose -f deploy/compose.prod.pinned.yml up -d --wait
```

Now the bytes Plynf is running are exactly the bytes you reviewed.

### What you get

- All 13 services on ports 7421–7433
- Persistent volume `plynf_data` for KV + files + workspace history
- Auto-restart on failure
- Healthchecks every 10 seconds
- Per-service logs via `docker compose logs -f <service>`

Full details on the image catalog, tags, SBOM, and architecture support live in [`deploy/registry.md`](deploy/registry.md).

---

## Path 3. From source

For when you want to modify Plynf itself.

### Prerequisites

- Python 3.11 or newer
- Git
- ~2 GB free disk for `.venv` and editable installs
- (Optional) Docker if you want to test the compose flow

### Setup

```bash
git clone https://github.com/nico-schindlbeck-jpg/plynf
cd plynf

make install     # creates .venv, pip-installs 14 packages editable
make test        # runs the suite (~2,800 tests, takes 2-5 min)
make serve       # starts all 13 services as background processes
make demo        # the headline 71%-fewer-tokens benchmark
```

The dashboard opens at `http://localhost:7424` (not 7420 — that's the embedded-mode unified port; in source mode, each service binds to its own port). All ports listed in `Makefile`.

Stop everything: `make stop`. Wipe data + logs: `make clean`.

### What this path is good for

- Building features upstream
- Debugging service interactions
- Writing your own MCP integrations
- Running the benchmark suite (`python -m benchmarks.workflows.run_simulation`)

---

## After install

Whichever path you took, your next steps are:

1. **Bootstrap a workspace.** The first time you open the dashboard, a 3-click wizard creates your first tenant + API key. Save the API key — it shows only once.
2. **Try the sample task.** The dashboard's top-level card offers "Run sample task" — a 5-source research workflow that produces the headline 71% reduction number on your machine.
3. **Connect a tool.** The "Tools" tab lists eight OAuth providers. The Block 7a release covers GitHub, Linear, Notion via the broker at `oauth.plynf.com` — no own app registration needed. The other five providers require you to bring your own client ID + secret for now (settings → Tools → "Use custom OAuth credentials").
4. **Write your first agent.** Pick the SDK matching your language:
   - Python: `pip install plynf-sdk`
   - TypeScript: `npm install @plynf/sdk`
   - Go: `go get github.com/nico-schindlbeck-jpg/plynf/sdk/go/plynf`
   - Swift: Swift Package Index (URL TBA at SDK launch)
   - Kotlin: Maven Central (URL TBA)

Each SDK ships a `Plinth` client class (note: class name kept for SDK backward compat — Phase-2 rebrand will alias to `Plynf`).

## Common problems

### Port already in use

Plynf binds to 7421–7433. If you have something else there:

```bash
# macOS / Linux
lsof -iTCP:7421-7433 -sTCP:LISTEN
# kill / reconfigure conflicting services, then retry
```

Override individual ports via env vars: `PLINTH_WORKSPACE_PORT=8421 make serve` (note: env vars still use `PLINTH_` prefix for runtime API compat).

### Docker pull fails with 401

The images are private until v1.6-launch. If you've been given a preview-access token:

```bash
echo "$YOUR_GITHUB_TOKEN" | docker login ghcr.io -u "$YOUR_GITHUB_USER" --password-stdin
```

After Plynf goes public, this step disappears — anonymous pulls will Just Work.

### The dashboard is blank or shows a Netlify error

Check `plynf doctor` — if it reports a service unhealthy, look at the service's log:

```bash
plynf logs --service dashboard --since 5m
# OR if you're on path 2
docker compose -f deploy/compose.prod.yml logs --tail 50 dashboard
```

### `make install` complains about a missing C compiler

Some Python packages have C extensions. On macOS install Xcode Command Line Tools (`xcode-select --install`). On Linux: `apt install build-essential` or distro equivalent.

## Uninstall

```bash
# Path 1
~/.plynf/install/uninstall.sh --purge

# Path 2
docker compose -f deploy/compose.prod.yml down -v

# Path 3
make clean              # data only
rm -rf .venv plynf/     # everything
```

The `--purge` flag wipes `~/.plynf`, the auto-start unit, and the `~/.local/bin/plynf` symlink. Without it, your data is preserved.

## What's still cooking

- **Embedded mode (Block 6)**: one binary, no Docker. Status: scaffold in `services/embedded/`, sibling-service `embedded=True` hooks landing soon. Will become Path 4 in this doc.
- **Tauri desktop app (Block 5)**: `.dmg` / `.msi` / `.AppImage` installers, native tray menu, auto-update. Deferred until product-market-fit signal — ~6-8 weeks.
- **`plynf` CLI (Block 3)**: Go binary that wraps `docker compose` and the embedded mode behind one command. `plynf up`, `plynf doctor`, `plynf oauth connect`. In flight.

If something here is unclear or broken, file an issue or write to hello@plynf.com.
