# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Driver-pluggable database abstraction for the workspace service.

This package adds Postgres support alongside the long-standing SQLite path.
The legacy ``plinth_workspace.db`` module continues to be the SQLite
implementation used by the existing storage / snapshots / channels stores —
nothing in this package is on the hot path for the v0.4 default deployment.

Usage:

    >>> from plinth_workspace.storage_drivers import create_database
    >>> db = create_database(settings)
    >>> await db.connect()
    >>> await db.init_schema()
    >>> # ... use db.execute/fetchone/fetchall/transaction ...
    >>> await db.close()

Each driver implements the :class:`Database` protocol from ``base``.
"""

from .base import Database
from .factory import create_database

__all__ = ["Database", "create_database"]
