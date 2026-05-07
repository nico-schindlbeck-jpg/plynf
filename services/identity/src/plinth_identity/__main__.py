# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Entrypoint: ``python -m plinth_identity``.

Subcommands:

* ``serve`` (default) — start the identity service via uvicorn.
* ``migrate`` — apply pending migrations, show status, scaffold new files.

Bare ``python -m plinth_identity`` (no subcommand) preserves the v0.3+
behaviour of starting the server.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import uvicorn

from .api import create_app
from .logging_config import configure_logging
from .migration_runner import (
    MigrationLockError,
    MigrationRunner,
    default_migrations_dir,
)
from .settings import get_settings


def _serve(_args: argparse.Namespace) -> int:
    """Run the identity service via uvicorn."""

    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)
    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.identity_host,
        port=settings.identity_port,
        log_level=settings.log_level.lower(),
        access_log=False,
    )
    return 0


def _migrate(args: argparse.Namespace) -> int:
    """Dispatch ``migrate`` subcommand actions."""

    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    migrations_dir = default_migrations_dir(__file__)
    runner = MigrationRunner(settings.db_path, migrations_dir)

    if args.create:
        path = runner.create_migration(args.create)
        print(f"created {path}")  # noqa: T201
        return 0

    if args.status:
        return asyncio.run(_print_status(runner))

    if args.to:
        return asyncio.run(_apply_to(runner, args.to))

    return asyncio.run(_apply_pending(runner))


async def _print_status(runner: MigrationRunner) -> int:
    status = await runner.status()
    print(f"current: {status.current or '(none)'}")  # noqa: T201
    print(f"applied: {len(status.applied)}")  # noqa: T201
    for mig in status.applied:
        print(  # noqa: T201
            f"  - {mig.id}  applied_at={mig.applied_at.isoformat()} "
            f"duration_ms={mig.duration_ms}"
        )
    print(f"pending: {len(status.pending)}")  # noqa: T201
    for mig in status.pending:
        print(f"  - {mig.id}")  # noqa: T201
    if status.mismatches:
        print(f"checksum mismatches: {len(status.mismatches)}")  # noqa: T201
        for mm in status.mismatches:
            print(  # noqa: T201
                f"  ! {mm.id}  stored={mm.stored_checksum[:12]}... "
                f"current={mm.current_checksum[:12]}..."
            )
        return 2
    return 0


async def _apply_pending(runner: MigrationRunner) -> int:
    try:
        applied = await runner.apply_pending()
    except MigrationLockError as exc:
        print(f"error: {exc}", file=sys.stderr)  # noqa: T201
        return 75
    if not applied:
        print("no pending migrations")  # noqa: T201
        return 0
    print(f"applied {len(applied)} migration(s):")  # noqa: T201
    for mig in applied:
        print(f"  - {mig.id}  duration_ms={mig.duration_ms}")  # noqa: T201
    return 0


async def _apply_to(runner: MigrationRunner, target: str) -> int:
    try:
        applied = await runner.apply_to(target)
    except MigrationLockError as exc:
        print(f"error: {exc}", file=sys.stderr)  # noqa: T201
        return 75
    if not applied:
        print(f"no pending migrations up to {target}")  # noqa: T201
        return 0
    print(f"applied {len(applied)} migration(s) up to {target}:")  # noqa: T201
    for mig in applied:
        print(f"  - {mig.id}  duration_ms={mig.duration_ms}")  # noqa: T201
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m plinth_identity",
        description="Plinth identity service — server + migrations.",
    )
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("serve", help="run the identity service (default)")

    mig = sub.add_parser("migrate", help="apply schema migrations")
    mig.add_argument(
        "--status",
        action="store_true",
        help="show applied + pending migrations and exit",
    )
    mig.add_argument(
        "--to",
        metavar="ID",
        help="apply forward migrations up to and including this ID",
    )
    mig.add_argument(
        "--create",
        metavar="LABEL",
        help="scaffold a new migration file with this label",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Top-level entrypoint.

    Returning the exit code (instead of calling ``sys.exit``) keeps the
    function easy to test. Bare ``python -m plinth_identity`` preserves the
    v0.3+ server-start behaviour.
    """

    parser = _build_parser()
    raw = list(sys.argv[1:] if argv is None else argv)

    if not raw or raw[0] not in {"serve", "migrate"}:
        return _serve(argparse.Namespace())

    args = parser.parse_args(raw)
    cmd = args.cmd or "serve"

    if cmd == "serve":
        return _serve(args)
    if cmd == "migrate":
        return _migrate(args)
    parser.error(f"unknown command: {cmd}")
    return 2  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
