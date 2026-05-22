# plynf-embedded

The five core Plynf services running in **one Python process**, packaged as a single static binary. Block 6 of the distribution roadmap.

## Why this exists

The Compose stack ships 13 containers and assumes Docker. For users who just want to try Plynf on their laptop with zero infra, that's a lot. The embedded runtime collapses the core into one binary:

- `workspace` — KV, files, snapshots, branches
- `gateway` — tool routing, caching, audit
- `identity` — JWT, tenants, JWKS
- `dashboard` — browser UI
- `mock-mcp` — mock tools so demos work

Everything binds to one port (`7420` by default), URL-mounted under `/_workspace`, `/_gateway`, `/_identity`, `/_mock`, and `/` for the dashboard.

What it does NOT include: the eight OAuth-MCP servers (GitHub, Slack, Linear, Notion, Google, Atlassian, Salesforce, Asana). Those need to spawn per-provider subprocesses and would balloon the binary past 600 MB. Users who need OAuth fall back to the Compose stack.

## Quick start (development)

```bash
# From repo root
make install   # installs all sibling service packages editable
cd services/embedded
pip install -e .

# Run
plynf-embedded
# → Dashboard at http://127.0.0.1:7420/
```

## Production build (PyInstaller)

```bash
cd services/embedded
pip install -e ".[dev]"
pyinstaller pyinstaller.spec --clean

# Output: dist/plynf-embedded (~200-280 MB)
./dist/plynf-embedded
```

Multi-platform builds run in CI (`.github/workflows/release-embedded.yml` — pending). Targets: macOS arm64, macOS x86_64, Windows x86_64, Linux arm64, Linux x86_64.

## Architecture (one-paragraph version)

We compose the five sub-apps using `FastAPI.mount()` plus an `AsyncExitStack`-driven lifespan. Starlette issue #649 (mounted sub-apps skip lifespan) is sidestepped by manually dispatching each sub-app's `router.lifespan_context` through the stack. Cross-service HTTP (e.g. gateway → identity for JWT verify) goes through `httpx.AsyncClient(transport=ASGITransport(app=sibling))` — same process, zero loopback TCP. See `docs/adr/0009-embedded-lifespan-strategy.md` for the alternatives we rejected and why.

## Limitations vs Compose mode

| Feature | Compose mode | Embedded mode |
|---|---|---|
| Service isolation | Each in own container | All in one process |
| OAuth MCP servers | All 8 supported | Mock-only |
| Restart granularity | Per service | Whole binary |
| Memory footprint | ~1.5 GB total | ~300-500 MB |
| Cold start | 30-90s (image pulls) | <5s (in-process boot) |
| Postgres support | Yes | SQLite only |
| Multi-tenancy at scale | Yes | Single-tenant pragmatic |

The right tool depends on what you're doing. For a laptop demo or a single-user side project: embedded. For a team / production / multi-tenancy: Compose.

## Files

```
services/embedded/
├── pyproject.toml                  # editable install + entry point
├── pyinstaller.spec                # frozen binary build config
├── README.md                       # this file
├── src/plynf_embedded/
│   ├── __init__.py                 # public API: make_embedded_app
│   ├── __main__.py                 # CLI entry: argparse + uvicorn.run
│   └── app.py                      # composer + lifespan + ASGI wiring
└── tests/
    └── test_smoke.py               # all 5 services boot, demo 01 runs
```

## Status

🟡 **Block 6 in progress.** Scaffolding committed; the `embedded=True` flag on each sibling service's `create_app()` needs follow-up PRs to land in those packages. PyInstaller spec is ready but untested against a real build. Track progress in the Block-E commits on `dist/block-e2-embedded-runtime`.
