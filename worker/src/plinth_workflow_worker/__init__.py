# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Plinth durable workflow worker.

A worker process polls the workspace service for pending workflow steps,
acquires a lease, dispatches to a registered handler, then releases the
lease — all coordinated via the v0.5 durable-executor primitives in the
workspace service.
"""

from __future__ import annotations

from .settings import WorkerSettings
from .worker import Worker

__version__ = "0.5.0"

__all__ = ["Worker", "WorkerSettings", "__version__"]
