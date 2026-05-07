# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Driver-pluggable database abstraction for the gateway service.

The legacy :class:`plinth_gateway.db.Database` keeps its v0.1+ aiosqlite
implementation untouched. This package adds a parallel surface that knows
how to speak Postgres too, selected at startup via ``PLINTH_STORAGE_DRIVER``.
"""

from .base import Database
from .factory import create_database

__all__ = ["Database", "create_database"]
