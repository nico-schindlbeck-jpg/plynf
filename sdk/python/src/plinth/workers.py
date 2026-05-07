# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Worker registration client for the durable workflow executor (v0.5).

Mirrors the workspace service's ``/v1/workers`` endpoints. The client is
attached to the :class:`Plinth` facade as ``client.workers`` and is
typically used only by the worker process and ops tooling — application
code rarely registers a worker directly.
"""

from __future__ import annotations

import os
import socket
from typing import TYPE_CHECKING

from .exceptions import WorkerNotFound
from .models import Worker

if TYPE_CHECKING:
    from .client import Plinth
    from ._http import HTTPClient


class WorkersClient:
    """Workspace-service ``/v1/workers`` client.

    The methods talk to the *workspace* service (where worker rows live)
    even though they're attached to the gateway-shaped :class:`Plinth`
    facade. Workers are workspace-scoped state, not gateway-scoped.
    """

    def __init__(self, http: HTTPClient) -> None:
        self._http = http

    def register(
        self,
        *,
        hostname: str | None = None,
        pid: int | None = None,
    ) -> Worker:
        """Register a new worker process.

        ``hostname`` and ``pid`` default to the current process's values
        so a typical worker just calls ``client.workers.register()``.
        """

        payload: dict = {
            "hostname": hostname if hostname is not None else socket.gethostname(),
            "pid": pid if pid is not None else os.getpid(),
        }
        response = self._http.post("/v1/workers/register", json=payload)
        return Worker.model_validate(response.json())

    def heartbeat(self, worker_id: str) -> Worker:
        """Bump ``last_heartbeat_at`` for ``worker_id``."""
        response = self._http.post(
            f"/v1/workers/{worker_id}/heartbeat",
            not_found_class=WorkerNotFound,
        )
        return Worker.model_validate(response.json())

    def drain(self, worker_id: str) -> Worker:
        """Mark ``worker_id`` as ``draining`` (graceful shutdown signal)."""
        response = self._http.post(
            f"/v1/workers/{worker_id}/drain",
            not_found_class=WorkerNotFound,
        )
        return Worker.model_validate(response.json())

    def list(self, *, status: str | None = None) -> list[Worker]:
        """List registered workers (optionally filtered by ``status``)."""
        params = {"status": status} if status else None
        data = self._http.get_json("/v1/workers", params=params)
        return [Worker.model_validate(w) for w in data.get("workers", [])]


__all__ = ["WorkersClient"]
