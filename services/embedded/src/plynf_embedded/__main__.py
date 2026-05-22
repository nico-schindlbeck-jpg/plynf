"""Entry point: launches the embedded Plynf runtime under uvicorn.

Two usage patterns:
  1. Installed via pip:
       plynf-embedded
  2. Frozen PyInstaller binary:
       ./plynf-embedded
       (the binary's main() routes here)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import uvicorn


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="plynf-embedded",
        description="Run all five core Plynf services in one process.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("PLYNF_EMBEDDED_HOST", "127.0.0.1"),
        help="Bind address (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PLYNF_EMBEDDED_PORT", "7420")),
        help="Listen port (default: 7420)",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("PLYNF_LOG_LEVEL", "info").lower(),
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="uvicorn log level",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Auto-reload on code changes (dev only)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("plynf.embedded")
    log.info("Starting Plynf embedded runtime on %s:%d", args.host, args.port)
    log.info("Dashboard will be available at http://%s:%d/", args.host, args.port)

    # Lazy import — keeps --help fast and lets us print friendly errors
    # if a sibling service is missing.
    try:
        from plynf_embedded.app import make_embedded_app
    except ImportError as e:
        sys.stderr.write(
            f"\n✘ failed to import a sibling service: {e}\n"
            f"  Run `make install` from the repo root, or `pip install -e .` in each of\n"
            f"  services/workspace, services/gateway, services/identity, services/dashboard,\n"
            f"  and mock-mcp-server.\n\n"
        )
        return 2

    uvicorn.run(
        "plynf_embedded.app:make_embedded_app",
        factory=True,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        reload=args.reload,
        access_log=False,  # individual services log their own access lines
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
