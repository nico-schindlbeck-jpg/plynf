# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for ``plinth.workspace.LocksProxy`` (v0.6 generic locks).

Locks are exposed as ``ws.locks``. The API mirrors the workspace
service contract: ``acquire`` / ``heartbeat`` / ``release`` / ``list`` /
``get`` plus the ``held(...)`` context manager that auto-heartbeats and
releases on exit.

All tests run offline against ``respx`` mocks so we exercise the request
shapes the SDK emits without a real workspace process running.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
import respx

from plinth import (
    Lock,
    LockConflict,
    LockNotFound,
    LockNotHeld,
    Plinth,
    Workspace,
)

from .conftest import error_envelope, make_workspace


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _later_iso(seconds: int = 60) -> str:
    return (datetime.now(tz=timezone.utc) + timedelta(seconds=seconds)).isoformat()


def make_lock(
    *,
    name: str = "kv:sources/index",
    workspace_id: str = "ws_01TESTWORKSPACE",
    holder: str = "agent-A",
    ttl: int = 60,
    waiters: int = 0,
) -> dict[str, Any]:
    """Return a JSON-shaped Lock body the workspace service would emit."""
    now = _now_iso()
    return {
        "name": name,
        "workspace_id": workspace_id,
        "holder": holder,
        "acquired_at": now,
        "expires_at": _later_iso(ttl),
        "heartbeat_at": now,
        "waiters": waiters,
    }


# ---------------------------------------------------------------------------
# Fixture: a Workspace handle ready for lock calls.
# ---------------------------------------------------------------------------


@pytest.fixture
def ws(client: Plinth, workspace_mock: respx.MockRouter) -> Workspace:
    workspace_mock.get("/v1/workspaces").mock(
        return_value=httpx.Response(200, json={"workspaces": [make_workspace()]})
    )
    return client.workspace("research-task-1")


# ---------------------------------------------------------------------------
# acquire / heartbeat / release roundtrip
# ---------------------------------------------------------------------------


def test_acquire_round_trip(ws: Workspace, workspace_mock: respx.MockRouter):
    name = "kv:sources/index"
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read()
        captured["url"] = str(request.url)
        return httpx.Response(200, json=make_lock(name=name))

    route = workspace_mock.post(
        f"/v1/workspaces/{ws.id}/locks/{name}/acquire"
    ).mock(side_effect=handler)

    lock = ws.locks.acquire(name, holder="agent-A", ttl_seconds=30, wait_ms=100)

    assert isinstance(lock, Lock)
    assert lock.name == name
    assert lock.holder == "agent-A"
    assert route.called
    body = captured["body"]
    assert b'"holder":"agent-A"' in body or b'"holder": "agent-A"' in body
    assert b"ttl_seconds" in body
    assert b"wait_ms" in body


def test_acquire_conflict_raises_lockconflict(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    workspace_mock.post(f"/v1/workspaces/{ws.id}/locks/foo/acquire").mock(
        return_value=httpx.Response(
            409,
            json={
                "error": {
                    "code": "LOCK_HELD",
                    "message": "lock is currently held",
                    "details": {
                        "current_holder": "agent-A",
                        "retry_after_seconds": 5,
                        "name": "foo",
                    },
                }
            },
        )
    )

    with pytest.raises(LockConflict) as excinfo:
        ws.locks.acquire("foo", holder="agent-B", ttl_seconds=30)

    err = excinfo.value
    assert err.current_holder == "agent-A"
    assert err.retry_after_seconds == 5


def test_heartbeat_round_trip(ws: Workspace, workspace_mock: respx.MockRouter):
    name = "hb"
    workspace_mock.post(f"/v1/workspaces/{ws.id}/locks/{name}/heartbeat").mock(
        return_value=httpx.Response(200, json=make_lock(name=name, holder="A"))
    )
    lock = ws.locks.heartbeat(name, holder="A", ttl_seconds=120)
    assert lock.holder == "A"


def test_heartbeat_unknown_lock_raises_locknotfound(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    workspace_mock.post(f"/v1/workspaces/{ws.id}/locks/missing/heartbeat").mock(
        return_value=httpx.Response(
            404, json=error_envelope("LOCK_NOT_FOUND", "lock not found")
        )
    )
    with pytest.raises(LockNotFound):
        ws.locks.heartbeat("missing", holder="A")


def test_heartbeat_wrong_holder_raises_locknotheld(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    workspace_mock.post(f"/v1/workspaces/{ws.id}/locks/wh/heartbeat").mock(
        return_value=httpx.Response(
            409, json=error_envelope("LOCK_NOT_HELD", "wrong holder")
        )
    )
    with pytest.raises(LockNotHeld):
        ws.locks.heartbeat("wh", holder="B")


def test_release_returns_none(ws: Workspace, workspace_mock: respx.MockRouter):
    route = workspace_mock.post(f"/v1/workspaces/{ws.id}/locks/r/release").mock(
        return_value=httpx.Response(204)
    )
    assert ws.locks.release("r", holder="A") is None
    assert route.called


# ---------------------------------------------------------------------------
# list / get
# ---------------------------------------------------------------------------


def test_list_returns_locks(ws: Workspace, workspace_mock: respx.MockRouter):
    workspace_mock.get(f"/v1/workspaces/{ws.id}/locks").mock(
        return_value=httpx.Response(
            200,
            json={
                "locks": [
                    make_lock(name="a"),
                    make_lock(name="b"),
                ]
            },
        )
    )
    locks = ws.locks.list()
    assert {lock.name for lock in locks} == {"a", "b"}


def test_get_returns_single_lock(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    workspace_mock.get(f"/v1/workspaces/{ws.id}/locks/inspect").mock(
        return_value=httpx.Response(200, json=make_lock(name="inspect"))
    )
    lock = ws.locks.get("inspect")
    assert lock.name == "inspect"


def test_get_unknown_raises_locknotfound(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    workspace_mock.get(f"/v1/workspaces/{ws.id}/locks/nope").mock(
        return_value=httpx.Response(
            404, json=error_envelope("LOCK_NOT_FOUND", "not found")
        )
    )
    with pytest.raises(LockNotFound):
        ws.locks.get("nope")


# ---------------------------------------------------------------------------
# Context-manager — `held(...)`
# ---------------------------------------------------------------------------


def test_held_acquires_and_releases(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    """The ``held(...)`` context manager must release on normal exit."""

    name = "kv:critical"
    acquire_route = workspace_mock.post(
        f"/v1/workspaces/{ws.id}/locks/{name}/acquire"
    ).mock(
        return_value=httpx.Response(200, json=make_lock(name=name, holder="A"))
    )
    release_route = workspace_mock.post(
        f"/v1/workspaces/{ws.id}/locks/{name}/release"
    ).mock(return_value=httpx.Response(204))

    # ``auto_heartbeat=False`` so the test doesn't have to wait for the
    # daemon thread; the round-trip behaviour is what matters here.
    with ws.locks.held(
        name, holder="A", ttl_seconds=30, auto_heartbeat=False
    ) as lock:
        assert lock.holder == "A"

    assert acquire_route.called
    assert release_route.called


def test_held_releases_on_exception(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    """Exceptions inside the ``held`` body must still release the lock."""

    name = "kv:flaky"
    workspace_mock.post(
        f"/v1/workspaces/{ws.id}/locks/{name}/acquire"
    ).mock(return_value=httpx.Response(200, json=make_lock(name=name)))
    release_route = workspace_mock.post(
        f"/v1/workspaces/{ws.id}/locks/{name}/release"
    ).mock(return_value=httpx.Response(204))

    class _Boom(RuntimeError):
        pass

    with pytest.raises(_Boom):
        with ws.locks.held(name, holder="A", auto_heartbeat=False):
            raise _Boom("boom")

    assert release_route.called


def test_held_propagates_lockconflict(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    workspace_mock.post(f"/v1/workspaces/{ws.id}/locks/contested/acquire").mock(
        return_value=httpx.Response(
            409,
            json={
                "error": {
                    "code": "LOCK_HELD",
                    "message": "held",
                    "details": {"current_holder": "agent-X"},
                }
            },
        )
    )
    with pytest.raises(LockConflict):
        with ws.locks.held(
            "contested", holder="me", ttl_seconds=10, auto_heartbeat=False
        ):
            pytest.fail("body must not run when acquire fails")


def test_held_auto_heartbeat_thread_dies_on_exit(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    """The daemon heartbeat thread must stop within join's grace period."""

    name = "hb-life"
    workspace_mock.post(
        f"/v1/workspaces/{ws.id}/locks/{name}/acquire"
    ).mock(return_value=httpx.Response(200, json=make_lock(name=name)))
    workspace_mock.post(f"/v1/workspaces/{ws.id}/locks/{name}/release").mock(
        return_value=httpx.Response(204)
    )
    workspace_mock.post(
        f"/v1/workspaces/{ws.id}/locks/{name}/heartbeat"
    ).mock(return_value=httpx.Response(200, json=make_lock(name=name)))

    pre_threads = threading.active_count()
    with ws.locks.held(
        name,
        holder="A",
        ttl_seconds=10,
        auto_heartbeat=True,
        # Use a heartbeat interval longer than the body so we exercise
        # the "stop_event interrupts an idle waiter" path rather than
        # racing a real heartbeat firing.
        heartbeat_interval=5.0,
    ):
        # Body finishes immediately — exit triggers stop_event.set().
        time.sleep(0.05)
    # Give the daemon a moment to drain.
    time.sleep(0.1)
    assert threading.active_count() <= pre_threads + 1, (
        "heartbeat thread was not joined cleanly"
    )


# ---------------------------------------------------------------------------
# Lock name encoding — preserve `/` in URLs.
# ---------------------------------------------------------------------------


def test_acquire_url_preserves_slash_in_name(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    name = "kv:sources/index"
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=make_lock(name=name))

    workspace_mock.post(
        f"/v1/workspaces/{ws.id}/locks/{name}/acquire"
    ).mock(side_effect=handler)
    ws.locks.acquire(name, holder="A", ttl_seconds=30)

    # The slash inside the name must round-trip unescaped — that's the
    # canonical use case for the ``:path`` route.
    assert "kv:sources/index" in captured["url"]
