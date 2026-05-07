# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Factory that picks a driver based on settings."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import Database
from .sqlite_driver import SQLiteDriver

if TYPE_CHECKING:  # pragma: no cover -- typing only
    from ..settings import Settings


def create_database(settings: "Settings") -> Database:
    """Construct the configured :class:`Database` driver.

    SQLite is the default, matching v0.1+ behaviour. Postgres is opt-in via
    ``PLINTH_STORAGE_DRIVER=postgres`` and requires ``PLINTH_DATABASE_URL``
    (or ``PLINTH_WORKSPACE_DATABASE_URL`` for service-specific overrides).
    """

    driver_name = (settings.storage_driver or "sqlite").lower()
    if driver_name == "sqlite":
        return SQLiteDriver(settings.db_path)
    if driver_name == "postgres":
        url = settings.effective_database_url
        if not url:
            raise RuntimeError(
                "PLINTH_STORAGE_DRIVER=postgres requires PLINTH_DATABASE_URL "
                "(or PLINTH_WORKSPACE_DATABASE_URL) to be set."
            )
        # Imported lazily so SQLite-only deployments don't pay the import.
        from .postgres_driver import PostgresDriver

        return PostgresDriver(
            url,
            min_size=settings.db_pool_min_size,
            max_size=settings.db_pool_max_size,
        )
    raise ValueError(
        f"Unknown PLINTH_STORAGE_DRIVER={driver_name!r}; "
        "expected 'sqlite' or 'postgres'."
    )


__all__ = ["create_database"]
