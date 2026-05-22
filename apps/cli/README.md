# plynf — Go CLI

The end-user command for running and managing a Plynf install. Single static binary, no Python or Docker required to invoke (Docker is invoked underneath if you use Compose mode).

## Why Go (and not Python)

The existing `cli/` Python package has ~10 subcommands and stays — for **dev-internal use** (CI, scripts, the demo runner). The Go CLI is **what end users install**, distributed as a 15 MB self-contained binary.

| | Go `plynf` | Python `plynf-dev` |
|---|---|---|
| Distribution | static binary | pip install editable |
| Bundle size | ~15 MB | ~100 MB venv |
| Startup | instant | 200-400 ms |
| Audience | end users | Plynf contributors |

## Commands

```
plynf up         start the stack (auto-detects Docker vs Embedded)
plynf down       stop the stack
plynf status     service health table
plynf logs       multiplexed colored log streaming
plynf doctor     diagnose problems (JSON or human output)
plynf update     pull new version + atomic rollback on failure
plynf oauth connect <provider>   open browser to authorize a tool
plynf uninstall  remove Plynf cleanly (--purge wipes data too)
```

Run `plynf <cmd> --help` for flags. Every command exits cleanly with a status code: 0 ok, 1 user-visible error, 2 internal-only.

## Modes

- **Docker** (default if Docker is reachable): wraps `docker compose -f deploy/compose.prod.yml`. Same 13 services as `compose up`, with a friendlier UX layer.
- **Embedded** (`--embedded` or auto-fallback if Docker missing): spawns the `plynf-embedded` binary that ships separately (Block 6 / E2). Single process, SQLite, no OAuth-MCPs.

## Status

🟡 **Scaffolded.** `up` and `down` work end-to-end (subject to having Docker or the embedded binary installed). `doctor` runs all checks. `logs`, `status`, `update`, `oauth connect`, `uninstall` are wired but their core logic lands in follow-up PRs.

## Build locally

```bash
cd apps/cli
go build -o plynf .
./plynf --help
```

## Release

Triggered automatically on `git tag v*.*.*` via `.github/workflows/release-cli.yml`. goreleaser produces:

- 6 archives: `plynf_<version>_<os>_<arch>.{tar.gz,zip}` for {darwin,linux,windows} × {amd64,arm64}
- `checksums.txt` (SHA-256)
- One SBOM per archive (`.spdx.json`)
- One cosign signature per artifact (`.sig`)
- A PR against `plynf/homebrew-tap` updating the formula

## Threading model

CLI is single-threaded for safety. Long-running operations (`update`, multi-service `logs`) spawn child processes via `os/exec.CommandContext` so Ctrl+C reliably propagates.

## Testing

```bash
go test ./...        # unit tests
go test -tags=e2e ./... # integration (needs Docker)
```

CI runs both on every PR.
