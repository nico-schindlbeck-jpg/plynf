# `plinth` — unified CLI

`plinth-cli` is the single command-line entry point for operating a
Plynf deployment. It consolidates service control, schema migrations,
workflow inspection, audit queries, tenant administration, and the
benchmark harness behind one ergonomic Click app.

```
$ plinth --help
Usage: plinth [OPTIONS] COMMAND [ARGS]...

  Plynf — unified ops + admin CLI.

  Manage services, run migrations, inspect workflows, query audit events,
  administer tenants, and run benchmarks from one place.

Options:
  -V, --version
  --profile NAME           Config profile (default: 'default').
  --config FILE            Override the config file path.
  --output [human|json]    Output format. Defaults to 'human' on TTY.
  --json                   Shortcut for --output=json.
  -h, --help

Commands:
  audit       Query the gateway audit log.
  bench       Run + compare benchmark suites.
  completion  Print or install shell completion scripts.
  config      Read/write the Plynf CLI config.
  health      Hit /healthz on every Plynf service.
  migrate     Apply schema migrations.
  services    Manage backing services.
  tenant      Administer tenants.
  workflow    List, inspect, cancel, and watch workflows.
```

## Install

From the monorepo (recommended for contributors):

```bash
pip install -e ./cli
```

From PyPI (once published):

```bash
pip install plinth-cli
```

The CLI requires Python 3.11+ and depends on the `plinth` SDK package.

## Quickstart

```bash
plinth config init                  # interactive ~/.plinth/config.toml
plinth services start               # spawn workspace + gateway + identity + …
plinth health                       # green checkmarks across the board
plinth workflow list                # list workflows across every workspace
plinth audit --since 1h             # last hour of tool invocations
```

Anything that prints a table also prints clean JSON when piped:

```bash
plinth health | jq '.workspace.ok'
```

## Configuration

`plinth` reads `~/.plinth/config.toml` (override with `--config PATH`).
Missing file? Built-in localhost defaults stand in. Run `plinth config
init` to scaffold one interactively.

```toml
[default]
workspace_url = "http://localhost:7421"
gateway_url   = "http://localhost:7422"
identity_url  = "http://localhost:7425"
api_key       = "local-dev"
output        = "human"

[profiles.production]
workspace_url = "https://workspace.plinth.example"
gateway_url   = "https://gateway.plinth.example"
identity_url  = "https://identity.plinth.example"
api_key_env   = "PLINTH_PROD_API_KEY"
output        = "json"
```

Switch profiles with `plinth --profile production health`.

Environment variables (`PLINTH_WORKSPACE_URL`, `PLINTH_GATEWAY_URL`,
`PLINTH_IDENTITY_URL`, `PLINTH_API_KEY`, `PLINTH_OUTPUT`,
`PLINTH_TIMEOUT`) override anything in the file. The `api_key_env`
indirection in profiles keeps secrets out of the config file.

## Commands at a glance

### `plinth services`

Spawn, stop, inspect, and tail logs for the backing services. Uses
`scripts/_spawn.py` from the monorepo so it shares the Makefile's PID/log
conventions (`/tmp/plinth-pids`, `/tmp/plinth-logs`).

```
plinth services start [name|all]
plinth services stop [name|all]
plinth services status
plinth services logs <name> [--tail N] [-f]
```

### `plinth migrate`

Drives the admin migration HTTP surface every service exposes at
`/v1/admin/migrations`.

```
plinth migrate status [<service>|all]
plinth migrate apply <service> [--to <id>]
plinth migrate rollback-to <service> <id> [--dry-run]
plinth migrate create <service> <label>      # surfaces the right code-side command
```

### `plinth workflow`

```
plinth workflow list [--workspace <id>] [--status <s>]
plinth workflow show <wf_id>
plinth workflow cancel <wf_id>
plinth workflow resume <wf_id>
plinth workflow watch <wf_id>      # poll-display until terminal
```

When `--workspace` is omitted, the CLI scans every workspace visible to
the bearer token.

### `plinth audit`

```
plinth audit [--tool ID] [--workspace WS] [--tenant T] [--since 1h] [--limit N]
plinth audit stats                  # aggregated last 24h
plinth audit tail                   # follow-mode (poll every 2s)
```

### `plinth tenant`

```
plinth tenant list
plinth tenant create <id> --name "Acme Corp"
plinth tenant show <id>
plinth tenant quotas <id> [--set max_workspaces=200 ...]
plinth tenant usage <id>
plinth tenant export <id> [--output ./acme-export.json]
plinth tenant delete <id> --confirm <token>
```

### `plinth health`

```
plinth health             # one-shot table; non-zero exit on any failure
plinth health watch       # re-poll every 2 seconds
```

### `plinth bench`

Delegates to the `plinth-bench` console script that ships in
`benchmarks/`. Install with `pip install -e ./benchmarks`.

```
plinth bench quick
plinth bench full
plinth bench compare baseline.json latest.json
```

### `plinth completion`

```
plinth completion show [--shell bash|zsh|fish]
plinth completion install [--shell bash|zsh|fish]
```

`install` writes a guarded block into your rc file (`~/.bashrc` /
`~/.zshrc` / `~/.config/fish/completions/plinth.fish`). Re-run with
`--force` to overwrite an existing block.

## Output modes

* `human` — rich tables and coloured status; default on a TTY.
* `json` — line-buffered JSON; default when stdout is piped.

Override with `--output json` or the `--json` shortcut. Every command
group produces the same data shape in both modes, so scripts can rely on
the JSON contract.

## Friendly errors

Connection failures surface as one-line errors, not stack traces:

```
$ plinth health
…
  ✘ Workspace  http://localhost:7421/healthz   not reachable
…
  3 ok, 1 failing
```

Exit code is non-zero whenever any probed service is failing, which
makes the CLI safe for cron + monitoring scripts.

## Testing

```bash
pip install -e ".[dev]"
pytest -q
```

Tests cover config resolution, every command group's happy path, JSON vs
human output, profile switching, error handling, and per-command help
text. They use `click.testing.CliRunner` + `respx` for HTTP mocking and
do not touch the network.

## License

Apache 2.0.
