#!/usr/bin/env python3
"""Plinth supervisor — spawn the five core services as children of this process.

Used by the launchd plist and the systemd unit. The supervisor's responsibilities
are intentionally small:

* Start each service as a subprocess with the right env vars + log redirection.
* Forward SIGTERM / SIGINT to children and wait for clean shutdown.
* If any child dies, log the reason and bring the rest down.
* Crash-loop guard — if any child crashes more than ``MAX_CRASHES`` times within
  ``CRASH_WINDOW_S``, exit with status 75 so the supervising service-manager
  (launchd / systemd) applies its own back-off and stops hammering.

Only stdlib — no extra deps.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ───────── Config ─────────
MAX_CRASHES = 3              # per service, within the window below
CRASH_WINDOW_S = 60          # seconds
SHUTDOWN_GRACE_S = 8         # SIGTERM grace before SIGKILL

PLINTH_HOME = Path(os.environ.get("PLINTH_HOME", str(Path.home() / ".plinth")))
LOGS_DIR = Path(os.environ.get("PLINTH_LOGS_DIR", str(PLINTH_HOME / "state" / "logs")))
DATA_DIR = Path(os.environ.get("PLINTH_DATA_DIR", str(PLINTH_HOME / "state" / "data")))
PIDS_DIR = Path(os.environ.get("PLINTH_PIDS_DIR", str(PLINTH_HOME / "state" / "pids")))

LOGS_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)
PIDS_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class Service:
    name: str
    module: str
    port: int
    extra_env: dict = field(default_factory=dict)
    # populated at runtime
    proc: Optional[subprocess.Popen] = None
    log_fp: object = None
    crashes: list = field(default_factory=list)  # timestamps


SERVICES = [
    Service(
        "workspace",
        "plinth_workspace",
        7421,
        {"PLINTH_WORKSPACE_PORT": "7421", "PLINTH_DATA_DIR": str(DATA_DIR)},
    ),
    Service(
        "gateway",
        "plinth_gateway",
        7422,
        {"PLINTH_GATEWAY_PORT": "7422", "PLINTH_DATA_DIR": str(DATA_DIR)},
    ),
    Service(
        "mock-mcp",
        "mock_mcp",
        7423,
        {"PLINTH_MOCK_PORT": "7423"},
    ),
    Service(
        "dashboard",
        "plinth_dashboard",
        7424,
        {
            "PLINTH_DASHBOARD_PORT": "7424",
            "PLINTH_DASHBOARD_WORKSPACE_URL": "http://localhost:7421",
            "PLINTH_DASHBOARD_GATEWAY_URL": "http://localhost:7422",
            "PLINTH_DASHBOARD_MOCK_MCP_URL": "http://localhost:7423",
        },
    ),
    Service(
        "identity",
        "plinth_identity",
        7425,
        {"PLINTH_IDENTITY_PORT": "7425", "PLINTH_IDENTITY_DATA_DIR": str(DATA_DIR)},
    ),
]

VENV_PY = Path(sys.executable)
SHUTTING_DOWN = False


def _log(msg: str) -> None:
    """Emit a supervisor log line — visible in launchd.out.log / systemd journal."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    print(f"[plinth-supervisor {ts}] {msg}", flush=True)


def _spawn(svc: Service) -> None:
    """Start one service. Logs go to <LOGS_DIR>/<svc>.log (append)."""
    log_path = LOGS_DIR / f"{svc.name}.log"
    pid_path = PIDS_DIR / f"{svc.name}.pid"

    svc.log_fp = open(log_path, "ab", buffering=0)

    env = os.environ.copy()
    env.update(svc.extra_env)
    env.setdefault("PYTHONUNBUFFERED", "1")

    svc.proc = subprocess.Popen(
        [str(VENV_PY), "-m", svc.module],
        stdout=svc.log_fp,
        stderr=svc.log_fp,
        stdin=subprocess.DEVNULL,
        env=env,
        close_fds=True,
        cwd=str(PLINTH_HOME),
    )
    pid_path.write_text(str(svc.proc.pid))
    _log(f"started {svc.name} (pid {svc.proc.pid}, port {svc.port})")


def _stop(svc: Service) -> None:
    """Stop one service. SIGTERM then SIGKILL after grace period."""
    if svc.proc is None:
        return
    if svc.proc.poll() is not None:
        return
    try:
        svc.proc.terminate()
    except ProcessLookupError:
        return

    deadline = time.monotonic() + SHUTDOWN_GRACE_S
    while time.monotonic() < deadline:
        if svc.proc.poll() is not None:
            break
        time.sleep(0.2)
    else:
        try:
            svc.proc.kill()
            svc.proc.wait(timeout=2)
        except Exception:  # noqa: BLE001
            pass

    if svc.log_fp is not None:
        try:
            svc.log_fp.close()
        except Exception:  # noqa: BLE001
            pass

    pid_path = PIDS_DIR / f"{svc.name}.pid"
    try:
        pid_path.unlink()
    except FileNotFoundError:
        pass


def _stop_all() -> None:
    for svc in SERVICES:
        _stop(svc)


def _signal_handler(signum, _frame) -> None:  # noqa: ANN001
    global SHUTTING_DOWN  # noqa: PLW0603
    if SHUTTING_DOWN:
        return
    SHUTTING_DOWN = True
    _log(f"caught signal {signum} — stopping services")
    _stop_all()


def _record_crash(svc: Service) -> bool:
    """Record a crash for ``svc`` and return ``True`` if we're past the threshold."""
    now = time.time()
    svc.crashes = [t for t in svc.crashes if t > now - CRASH_WINDOW_S]
    svc.crashes.append(now)
    return len(svc.crashes) > MAX_CRASHES


def main() -> int:
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    # SIGHUP: re-load not yet supported — treat as shutdown.
    try:
        signal.signal(signal.SIGHUP, _signal_handler)
    except (AttributeError, ValueError):
        pass  # Windows / restricted env

    _log(f"PLINTH_HOME={PLINTH_HOME}  python={VENV_PY}")
    _log(f"starting {len(SERVICES)} services")
    for svc in SERVICES:
        _spawn(svc)

    # Supervise.
    try:
        while not SHUTTING_DOWN:
            time.sleep(1)

            for svc in SERVICES:
                if svc.proc is None or SHUTTING_DOWN:
                    continue
                rc = svc.proc.poll()
                if rc is None:
                    continue
                _log(f"service '{svc.name}' exited with code {rc}")
                if _record_crash(svc):
                    _log(
                        f"service '{svc.name}' crashed {len(svc.crashes)}x in "
                        f"{CRASH_WINDOW_S}s — giving up; let launchd/systemd back off"
                    )
                    _stop_all()
                    return 75  # EX_TEMPFAIL
                _log(f"restarting '{svc.name}'")
                try:
                    _spawn(svc)
                except Exception as exc:  # noqa: BLE001
                    _log(f"  failed to restart '{svc.name}': {exc!r}")
                    _stop_all()
                    return 1
    finally:
        _stop_all()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
