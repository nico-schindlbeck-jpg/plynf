# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Driver-pluggable database abstraction for the identity service.

The legacy :mod:`plinth_identity.store` keeps its v0.3 aiosqlite path.
This package adds a parallel surface that knows Postgres, selected at
startup via ``PLINTH_STORAGE_DRIVER``.
"""

from .base import Database
from .factory import create_database

__all__ = ["Database", "create_database"]
