# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""In-memory revocation cache, refreshed from the identity service.

The gateway verifies JWTs locally (HS256 via shared secret, RS256 via
JWKS). Local verification is fast but means a *revocation* on a peer
identity replica isn't visible until that replica's state propagates
here. This cache closes the loop:

* On startup, fetch the full revocation list from
  ``GET /v1/revocations`` (since=0).
* Every ``poll_interval`` seconds, fetch new entries with the cursor
  returned by the previous poll (``next_since``).
* The auth middleware consults :meth:`is_revoked` after a successful JWT
  decode and rejects with ``TOKEN_REVOKED`` on a hit.

If polling fails (Identity unreachable, network blip), the cache stays
as-is and the failure is captured in :attr:`stats`. Revocations newly
issued on Identity won't propagate until polling resumes; this is an
explicit, documented limitation — the alternative (failing closed) would
turn an Identity outage into a gateway outage, which is worse for most
deployments.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

UTC = timezone.utc  # noqa: UP017

# How long to wait for the polling HTTP request before giving up on this
# tick. Failures don't break verification — see the module docstring.
_POLL_TIMEOUT_SECONDS = 10.0


class RevocationCache:
    """In-memory ``set[str]`` of revoked JTIs, refreshed by polling Identity.

    Thread-safety: the cache assumes a single asyncio event loop owner.
    All mutations happen inside the polling task. Reads
    (:meth:`is_revoked`) are pure ``set`` membership checks, safe to call
    from any coroutine on the same loop.
    """

    def __init__(
        self,
        *,
        identity_url: str,
        poll_interval: int = 60,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._identity_url = identity_url.rstrip("/") if identity_url else ""
        self._poll_interval = max(1, int(poll_interval))
        self._revoked: set[str] = set()
        self._cursor: int = 0
        self._task: asyncio.Task[None] | None = None
        # ``asyncio.Event`` must be constructed inside a running event loop
        # in Python 3.9 (otherwise it binds to a getter that may resolve to
        # a different loop than ``start()`` runs on, silently dropping the
        # background poll task). Defer creation to ``start()``.
        self._stopping: asyncio.Event | None = None
        self._last_poll_at: datetime | None = None
        self._last_poll_error: str | None = None
        # An optional client lets tests inject a respx-mounted instance.
        self._http: httpx.AsyncClient | None = http_client
        self._owned_http = http_client is None

    # ------------------------------------------------------------------ API

    @property
    def identity_url(self) -> str:
        return self._identity_url

    @property
    def poll_interval(self) -> int:
        return self._poll_interval

    def is_revoked(self, jti: str | None) -> bool:
        """Pure in-memory check. Returns False for ``None`` for ergonomic use."""

        if not jti:
            return False
        return jti in self._revoked

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "size": len(self._revoked),
            "cursor": self._cursor,
            "last_poll_at": (
                self._last_poll_at.isoformat() if self._last_poll_at else None
            ),
            "last_poll_error": self._last_poll_error,
            "running": self._task is not None and not self._task.done(),
            "identity_url": self._identity_url,
            "poll_interval": self._poll_interval,
        }

    # ----------------------------------------------------------------- I/O

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=_POLL_TIMEOUT_SECONDS)
        return self._http

    async def _poll_once(self) -> None:
        """Fetch new revocations since the last cursor; update set + cursor.

        Failures are logged + recorded in :attr:`stats` but never raised.
        """

        if not self._identity_url:
            self._last_poll_error = "identity_url not configured"
            return

        url = f"{self._identity_url}/v1/revocations"
        try:
            client = await self._client()
            response = await client.get(url, params={"since": self._cursor})
            response.raise_for_status()
            data = response.json()
            for entry in data.get("revocations", []):
                jti = entry.get("jti")
                if jti:
                    self._revoked.add(jti)
            try:
                self._cursor = int(data.get("next_since", self._cursor))
            except (TypeError, ValueError):
                pass
            self._last_poll_at = datetime.now(UTC)
            self._last_poll_error = None
            structlog.get_logger().debug(
                "revocation_cache.poll_ok",
                size=len(self._revoked),
                cursor=self._cursor,
                added=len(data.get("revocations", [])),
            )
        except Exception as exc:  # noqa: BLE001
            self._last_poll_error = str(exc)
            structlog.get_logger().warning(
                "revocation_cache.poll_failed",
                error=str(exc),
                url=url,
            )

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        """Start the background polling loop. Idempotent.

        Performs an initial synchronous poll so the cache is warm before
        the first request hits the auth middleware.
        """

        if self._task is not None and not self._task.done():
            return
        # Bind the Event to the *current* event loop. Doing this inside an
        # async method guarantees that ``self._stopping`` and the task we
        # spawn below share the same loop — Python 3.9's
        # ``get_event_loop()`` semantics make eager construction in
        # ``__init__`` racy across threaded uvicorn deployments.
        self._stopping = asyncio.Event()
        await self._poll_once()

        stopping = self._stopping

        async def _loop() -> None:
            while not stopping.is_set():
                try:
                    await asyncio.wait_for(
                        stopping.wait(),
                        timeout=self._poll_interval,
                    )
                except asyncio.TimeoutError:
                    pass
                if stopping.is_set():
                    break
                await self._poll_once()

        self._task = asyncio.create_task(
            _loop(), name="plinth-gateway-revocation-poll"
        )

    async def stop(self) -> None:
        """Stop the polling loop and close the HTTP client (if owned)."""

        if self._stopping is not None:
            self._stopping.set()
        task = self._task
        self._task = None
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except asyncio.TimeoutError:  # pragma: no cover - defensive
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        if self._owned_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    # ------------------------------------------------------------------ test

    def _force_revoke(self, jti: str) -> None:
        """Test-only helper: pre-populate a JTI without polling."""

        self._revoked.add(jti)


__all__ = ["RevocationCache"]
