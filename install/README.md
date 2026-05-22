# Plynf installer

This directory implements **Stufe 1** — the one-liner installer. End users do not
need to read this file; it's a transparency log for operators who want to know
exactly what the script does before they pipe it to `sh`.

## TL;DR

```bash
curl -fsSL https://plynf.com/install.sh | sh
```

Installs Plynf to `~/.plinth/`, drops a `plinth` CLI in `~/.local/bin/`,
registers a launchd (macOS) or systemd `--user` (Linux) auto-start unit, and
opens the dashboard at <http://localhost:7424>. Total runtime ~2 minutes on a
warm pip cache.

## File map

| Path | What it is |
|------|------------|
| `install.sh` | The POSIX shell script that `curl … \| sh` pipes to. |
| `plinth` | The CLI wrapper installed at `~/.local/bin/plinth`. |
| `uninstall.sh` | Reverses every change `install.sh` makes. |
| `run_all.py` | Tiny supervisor — spawns 5 core services as children. |
| `launchd/dev.plinth.services.plist.template` | macOS launchd unit (templated). |
| `systemd/plinth.service.template` | Linux systemd `--user` unit (templated). |
| `tests/test_install_sh.sh` | Test harness — syntax check, idempotency, OS detection. |
| `tests/shellcheck.sh` | Optional shellcheck pass (skipped if not installed). |

## Where Plynf lives after install

```
~/.plinth/
├── repo/                 # git checkout (shallow, branch: main)
├── venv/                 # Python 3.11+ venv with all services installed editable
└── state/
    ├── data/             # workspace KV + files + DB
    ├── logs/             # one log per service + supervisor log
    └── pids/             # per-service pid files (managed by run_all.py)

~/.local/bin/plinth       # CLI wrapper

# macOS:
~/Library/LaunchAgents/dev.plinth.services.plist

# Linux:
~/.config/systemd/user/plinth.service
```

Nothing is written outside the user's home directory. No sudo is required for
the default install path.

## What the installer touches in your shell

If `~/.local/bin` is not already in `PATH`, a single block is appended to your
shell rc file (`.zshrc` / `.bashrc` / `.profile` depending on `$SHELL`):

```sh
# >>> Plynf installer >>>
# This block was added by the Plynf installer. To remove it, run
#   plinth uninstall
export PATH="$HOME/.local/bin:$PATH"
# <<< Plynf installer <<<
```

The markers are recognised by the uninstaller so removal is exact.

## Flags

```sh
sh install/install.sh --help
```

| Flag | Effect |
|------|--------|
| `--verbose` | Print every step + every command run. |
| `--skip-autostart` | Don't touch launchd/systemd. Useful in containers. |
| `--skip-services` | Don't `pip install` the editable packages (CI test mode). |
| `--skip-open` | Don't open the dashboard at the end. |
| `--no-update` | If `~/.plinth/repo` exists, skip `git fetch`. |
| `--dry-run` | Print what would happen but make no changes. |

Environment overrides: `PLINTH_HOME`, `PLINTH_BIN_DIR`, `PLINTH_REPO_URL`,
`PLINTH_REF`, `PLINTH_PYTHON`.

## CLI reference

```text
plinth status         health-check all services
plinth start          start the stack (via launchd/systemd)
plinth stop           stop the stack
plinth restart        stop + start
plinth logs [svc]     tail a service log (default: workspace)
plinth dashboard      open http://localhost:7424
plinth demo [topic]   run the headline token-comparison demo
plinth update         git pull + re-install + restart
plinth uninstall      remove Plynf from this system
plinth version        print installed version
```

## Troubleshooting

### `plinth: command not found` after install

Your shell hasn't re-read its rc file yet. Either:

```sh
source ~/.zshrc      # zsh
source ~/.bashrc     # bash
# or just open a new terminal tab
```

### Services not starting on macOS

Check launchd's view of the agent:

```sh
launchctl list | grep plinth
tail -f ~/.plinth/state/logs/launchd.err.log
tail -f ~/.plinth/state/logs/workspace.log
```

If `KeepAlive` is keeping it in a crash loop, the supervisor exits with code
75 after 3 crashes in 60 s and launchd will back off (`ThrottleInterval=5`).

### Services not surviving logout on Linux

Plynf ships as a `systemd --user` unit. To keep `--user` units running when
the user is not logged in, enable linger (requires sudo, one-time):

```sh
sudo loginctl enable-linger "$USER"
```

The installer prints this hint if linger is off.

### Crash-loop / port in use

Stop any conflicting process:

```sh
plinth stop
# Then check ports 7421-7425 with: lsof -i :7421
```

### Resetting state

```sh
plinth stop
rm -rf ~/.plinth/state
plinth start
```

### Uninstalling completely

```sh
plinth uninstall          # interactive — asks before removing ~/.plinth
plinth uninstall --purge  # nuke ~/.plinth too, no prompt
```

## Security notes

* The install script only requires user-level permissions — never `sudo`.
* No telemetry. The script makes outbound connections only to `github.com`
  (to clone) and PyPI (for pip installs).
* The supervisor (`run_all.py`) binds services to `localhost`. They are not
  exposed on the network.
* The launchd / systemd units run as the invoking user only, not root.
* All state — including any secrets the user adds via Plynf itself — lives
  under `~/.plinth/state/` and is removed by `plinth uninstall --purge`.

## Verification (for CI)

```sh
# Syntax check
sh -n install/install.sh
sh -n install/uninstall.sh
sh -n install/plinth

# Lint (where available)
shellcheck -s sh install/install.sh
shellcheck -s sh install/uninstall.sh
shellcheck -s sh install/plinth

# Self-test
sh install/tests/test_install_sh.sh
```

The CI job `installer` in `.github/workflows/ci.yml` runs all of the above on
every PR.
