# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""CLI entry point for the durable workflow worker.

Usage::

    plinth-workflow-worker \\
        --workspace-url http://localhost:7421 \\
        --gateway-url http://localhost:7422 \\
        --identity-url http://localhost:7425 \\
        --api-key "..." \\
        --concurrency 4 \\
        --lease-ttl 60 \\
        --heartbeat-interval 15 \\
        --handlers-module myapp.handlers

The ``--handlers-module`` is imported via ``importlib.import_module(...)``;
the imported module is expected to register handlers via
``@client.workflow_handler(workflow, step=step)`` so the dispatch table
is populated before the worker starts polling.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import sys

from plinth import Plinth

from . import __version__
from .logging_config import configure_logging, get_logger
from .settings import WorkerSettings
from .worker import Worker


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="plinth-workflow-worker",
        description=(
            "Plinth durable workflow worker — leases pending workflow "
            "steps and runs registered handlers."
        ),
    )
    parser.add_argument("--workspace-url", help="Workspace service URL.")
    parser.add_argument("--gateway-url", help="Gateway service URL.")
    parser.add_argument(
        "--identity-url",
        default=None,
        help="Identity service URL (optional).",
    )
    parser.add_argument("--api-key", help="Bearer token for both services.")
    parser.add_argument(
        "--concurrency",
        type=int,
        help="Number of concurrent in-flight steps (default 4).",
    )
    parser.add_argument(
        "--lease-ttl",
        type=int,
        help="Lease TTL in seconds when leasing (default 60).",
    )
    parser.add_argument(
        "--heartbeat-interval",
        type=int,
        help="Per-lease heartbeat interval in seconds (default 15).",
    )
    parser.add_argument(
        "--worker-heartbeat-interval",
        type=int,
        help="Worker-level heartbeat interval in seconds (default 30).",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        help="Idle poll interval when no work is available (default 2.0).",
    )
    parser.add_argument(
        "--handlers-module",
        help=(
            "Importable Python module path that registers workflow "
            "handlers (e.g. ``myapp.handlers``)."
        ),
    )
    parser.add_argument(
        "--workspace",
        action="append",
        dest="workspace_filter",
        default=None,
        help=(
            "Restrict the worker to the named workspace (can be passed "
            "multiple times). When omitted, every workspace visible to "
            "the API key is scanned."
        ),
    )
    parser.add_argument(
        "--log-level",
        default=None,
        help="One of DEBUG / INFO / WARNING / ERROR.",
    )
    parser.add_argument(
        "--log-format",
        choices=("console", "json"),
        default=None,
        help="Log renderer: console (dev) or json (prod).",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"plinth-workflow-worker {__version__}",
    )
    return parser


def _build_settings(args: argparse.Namespace) -> WorkerSettings:
    """Layer CLI flags on top of env-driven defaults."""

    overrides: dict = {}
    for cli_field, settings_field in [
        ("workspace_url", "workspace_url"),
        ("gateway_url", "gateway_url"),
        ("identity_url", "identity_url"),
        ("api_key", "api_key"),
        ("concurrency", "concurrency"),
        ("lease_ttl", "lease_ttl"),
        ("heartbeat_interval", "heartbeat_interval"),
        ("worker_heartbeat_interval", "worker_heartbeat_interval"),
        ("poll_interval", "poll_interval"),
        ("handlers_module", "handlers_module"),
        ("log_level", "log_level"),
        ("log_format", "log_format"),
    ]:
        v = getattr(args, cli_field, None)
        if v is not None:
            overrides[settings_field] = v
    return WorkerSettings(**overrides)


def _import_handlers(module_path: str) -> None:
    """Import the user's handlers module so its decorations execute."""

    if not module_path:
        return
    importlib.import_module(module_path)


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    settings = _build_settings(args)

    configure_logging(level=settings.log_level, fmt=settings.log_format)
    log = get_logger()

    if not settings.handlers_module:
        log.error(
            "worker.handlers_module_missing",
            hint=(
                "Pass --handlers-module myapp.handlers (or set "
                "PLINTH_HANDLERS_MODULE) so the worker can populate "
                "its dispatch table."
            ),
        )
        return 2

    log.info(
        "worker.boot",
        version=__version__,
        workspace_url=settings.workspace_url,
        gateway_url=settings.gateway_url,
        concurrency=settings.concurrency,
        lease_ttl=settings.lease_ttl,
        heartbeat_interval=settings.heartbeat_interval,
        handlers_module=settings.handlers_module,
    )

    # Importing the handlers module triggers @workflow_handler
    # registrations against whichever Plinth client the user
    # constructed inside that module. We then build OUR client below
    # but reach for the same registry by re-using the module's client
    # if it exposes one — otherwise we register against ours.
    _import_handlers(settings.handlers_module)

    client = Plinth(
        workspace_url=settings.workspace_url,
        gateway_url=settings.gateway_url,
        identity_url=settings.identity_url,
        api_key=settings.api_key,
    )

    # If the imported handlers module defined its own ``client`` with
    # registrations, re-use that runtime so the worker actually finds
    # the handlers. Otherwise fall back to ``client._workflow_runtime``
    # (empty unless the handlers used our ``client`` instance).
    runtime = client.workflow_runtime
    handlers_mod = sys.modules.get(settings.handlers_module)
    if handlers_mod is not None:
        for attr in vars(handlers_mod).values():
            if isinstance(attr, Plinth):
                runtime = attr.workflow_runtime
                client = attr
                break

    worker = Worker(
        client,
        runtime=runtime,
        concurrency=settings.concurrency,
        lease_ttl=settings.lease_ttl,
        heartbeat_interval=settings.heartbeat_interval,
        worker_heartbeat_interval=settings.worker_heartbeat_interval,
        poll_interval=settings.poll_interval,
        workspace_filter=args.workspace_filter,
    )

    async def _run() -> None:
        worker.install_signal_handlers()
        await worker.run()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        log.info("worker.interrupted")
        return 130
    finally:
        client.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
