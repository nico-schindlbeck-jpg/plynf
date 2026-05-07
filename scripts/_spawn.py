#!/usr/bin/env python3
"""Cross-platform service spawner that survives the parent shell's exit.

Usage:
    python scripts/_spawn.py <pid_file> <log_file> <env_var>=<val>... -- <cmd>...

Used by the Makefile to start workspace/gateway/mock-mcp without requiring
``setsid`` (which doesn't exist on macOS) and without nohup quirks.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    if "--" not in sys.argv:
        print("usage: _spawn.py <pid_file> <log_file> [VAR=VAL ...] -- <cmd> [args...]", file=sys.stderr)
        return 2

    sep = sys.argv.index("--")
    head = sys.argv[1:sep]
    cmd = sys.argv[sep + 1 :]

    if len(head) < 2:
        print("error: need at least <pid_file> <log_file>", file=sys.stderr)
        return 2

    pid_file = Path(head[0])
    log_file = Path(head[1])
    env_pairs = head[2:]

    env = os.environ.copy()
    for pair in env_pairs:
        if "=" not in pair:
            print(f"error: bad env pair {pair!r}", file=sys.stderr)
            return 2
        key, _, val = pair.partition("=")
        env[key] = val

    pid_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    # Open log for append; line-buffered so 'tail -f' is responsive.
    with open(log_file, "ab", buffering=0) as log_fp:
        proc = subprocess.Popen(
            cmd,
            stdout=log_fp,
            stderr=log_fp,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
            close_fds=True,
        )

    pid_file.write_text(str(proc.pid))
    print(proc.pid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
