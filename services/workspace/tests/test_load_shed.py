# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Load-shedding middleware tests for the workspace service.

Spec (CONTRACTS.md → v0.5 → Stress Benchmarks + Load-Shedding):

* default ``load_shed_enabled=False`` → no behaviour change
* over capacity → 503 + ``Retry-After``
* ``/healthz`` always passes regardless of shed state
* admin stats endpoint surfaces counters
* shed counter increments on rejection
* memory bound: queue cap is enforced (no unbounded growth)
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from plinth_workspace.api import create_app
from plinth_workspace.db import init_db
from plinth_workspace.load_shed import LoadShedder, OverloadedError
from plinth_workspace.settings import Settings


# ---------------------------------------------------------------------------
# Unit: LoadShedder bookkeeping
# ---------------------------------------------------------------------------


async def test_shedder_admits_under_inflight_cap() -> None:
    """All ``acquire`` calls succeed when below the inflight cap."""

    shedder = LoadShedder(max_inflight=3, max_queue=0, enabled=True)
    async with shedder.acquire():
        async with shedder.acquire():
            async with shedder.acquire():
                stats = shedder.stats
                assert stats["inflight"] == 3
                assert stats["queued"] == 0
                assert stats["shed_count"] == 0


async def test_shedder_routes_to_queue_when_inflight_full() -> None:
    """Admit at most ``max_inflight``, then queue up to ``max_queue``."""

    shedder = LoadShedder(max_inflight=2, max_queue=3, enabled=True)

    # Acquire two slots that we hold open while we test queue routing.
    held: list[asyncio.Future] = []

    async def hold(slot_idx: int) -> None:
        async with shedder.acquire():
            held.append(asyncio.get_running_loop().create_future())
            await held[-1]  # block until released

    t1 = asyncio.create_task(hold(0))
    t2 = asyncio.create_task(hold(1))
    # Wait for both to enter their context managers.
    while shedder.stats["inflight"] < 2:
        await asyncio.sleep(0)

    # Now any further ``acquire`` should be tagged as queued.
    async with shedder.acquire():
        assert shedder.stats["queued"] == 1
    # Released → queue back to zero.
    assert shedder.stats["queued"] == 0

    # Release the held tasks so they finish cleanly.
    for f in held:
        f.set_result(None)
    await asyncio.gather(t1, t2)
    assert shedder.stats["inflight"] == 0


async def test_shedder_rejects_when_inflight_and_queue_full() -> None:
    """Over capacity → ``OverloadedError`` with retry hint, no leak."""

    shedder = LoadShedder(
        max_inflight=1,
        max_queue=1,
        retry_after_seconds=7,
        enabled=True,
    )

    held: asyncio.Future
    queue_held: asyncio.Future

    async def hold_inflight() -> None:
        nonlocal held
        async with shedder.acquire():
            held = asyncio.get_running_loop().create_future()
            await held

    async def hold_queue() -> None:
        nonlocal queue_held
        async with shedder.acquire():
            queue_held = asyncio.get_running_loop().create_future()
            await queue_held

    t_in = asyncio.create_task(hold_inflight())
    while shedder.stats["inflight"] < 1:
        await asyncio.sleep(0)
    t_q = asyncio.create_task(hold_queue())
    while shedder.stats["queued"] < 1:
        await asyncio.sleep(0)

    # Now both inflight + queue are at capacity → next acquire raises.
    with pytest.raises(OverloadedError) as exc:
        async with shedder.acquire():
            pytest.fail("should not enter the body")
    assert exc.value.retry_after == 7
    assert shedder.stats["shed_count"] == 1

    # Release everything cleanly so no warnings about pending tasks.
    held.set_result(None)
    queue_held.set_result(None)
    await asyncio.gather(t_in, t_q)


async def test_shedder_disabled_property_passthrough() -> None:
    """``enabled=False`` doesn't disable acquire (unit-level), but the
    middleware will read ``enabled`` and bypass entirely."""

    shedder = LoadShedder(max_inflight=1, max_queue=1, enabled=False)
    assert shedder.stats["enabled"] is False


# ---------------------------------------------------------------------------
# Integration: middleware against the FastAPI app
# ---------------------------------------------------------------------------


@pytest.fixture()
def shed_settings(tmp_path: Path) -> Settings:
    """Settings with load-shed enabled at very small caps for testing."""

    return Settings(
        data_dir=tmp_path / "data",
        workspace_port=17421,
        workspace_host="127.0.0.1",
        log_level="WARNING",
        log_format="console",
        auth_required=False,
        load_shed_enabled=True,
        load_shed_max_inflight=2,
        load_shed_max_queue=1,
        load_shed_retry_after_seconds=2,
    )


@pytest_asyncio.fixture()
async def shed_client(shed_settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    shed_settings.data_dir.mkdir(parents=True, exist_ok=True)
    shed_settings.blobs_dir.mkdir(parents=True, exist_ok=True)
    await init_db(shed_settings.db_path)

    app = create_app(shed_settings)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer test-token"},
    ) as c:
        yield c


async def test_disabled_no_shedding_under_burst(client: httpx.AsyncClient) -> None:
    """Default settings (shed disabled) → every request lands."""

    # Fire 20 healthz + 20 list-workspace concurrently; all should 200/201/etc.
    coros = [client.get("/healthz") for _ in range(20)]
    coros += [client.get("/v1/workspaces") for _ in range(20)]
    results = await asyncio.gather(*coros)
    assert all(r.status_code == 200 for r in results)


async def test_enabled_below_threshold_passes(shed_client: httpx.AsyncClient) -> None:
    """Enabled but lightly loaded → no 503s."""

    # max_inflight=2, max_queue=1 → up to 2 concurrent should pass.
    r1 = await shed_client.get("/healthz")
    r2 = await shed_client.get("/v1/workspaces")
    assert r1.status_code == 200
    assert r2.status_code == 200


async def test_healthz_always_passes_under_shed(
    shed_settings: Settings,
) -> None:
    """``/healthz`` is exempt — even when the shedder is at capacity."""

    shed_settings.data_dir.mkdir(parents=True, exist_ok=True)
    shed_settings.blobs_dir.mkdir(parents=True, exist_ok=True)
    await init_db(shed_settings.db_path)

    app = create_app(shed_settings)
    # Manually saturate the shedder by pinning fake counters; healthz should
    # still pass because the middleware checks the path before acquiring.
    shedder = app.state.load_shedder
    shedder._inflight = shedder.max_inflight  # noqa: SLF001
    shedder._queued = shedder.max_queue  # noqa: SLF001

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer test-token"},
    ) as c:
        # Even though the shedder is "full", healthz still passes.
        r = await c.get("/healthz")
        assert r.status_code == 200
        # And a non-health request would shed (we simulate it by direct
        # call to ``acquire`` because the asgi transport runs requests
        # serially per client in tests, making concurrency tricky).
        with pytest.raises(OverloadedError):
            async with shedder.acquire():
                pytest.fail("should not enter")


async def test_overload_returns_503_with_retry_after(
    shed_settings: Settings,
) -> None:
    """A request that hits the shed limit gets 503 + ``Retry-After``."""

    shed_settings.data_dir.mkdir(parents=True, exist_ok=True)
    shed_settings.blobs_dir.mkdir(parents=True, exist_ok=True)
    await init_db(shed_settings.db_path)

    app = create_app(shed_settings)
    # Pre-saturate counters so the next request gets shed.
    shedder = app.state.load_shedder
    shedder._inflight = shedder.max_inflight  # noqa: SLF001
    shedder._queued = shedder.max_queue  # noqa: SLF001

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer test-token"},
    ) as c:
        r = await c.get("/v1/workspaces")
    assert r.status_code == 503
    assert r.headers["Retry-After"] == "2"
    body = r.json()
    assert body["error"]["code"] == "OVERLOADED"
    assert body["error"]["details"]["retry_after_seconds"] == 2
    assert shedder.stats["shed_count"] == 1


async def test_admin_stats_endpoint(shed_client: httpx.AsyncClient) -> None:
    """``/v1/admin/load-shed/stats`` returns the counters."""

    r = await shed_client.get("/v1/admin/load-shed/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["max_inflight"] == 2
    assert body["max_queue"] == 1
    assert body["enabled"] is True
    assert body["shed_count"] == 0
    assert "inflight" in body
    assert "queued" in body


async def test_admin_stats_reports_shed_count(
    shed_settings: Settings,
) -> None:
    """The shed_count increments after a 503."""

    shed_settings.data_dir.mkdir(parents=True, exist_ok=True)
    shed_settings.blobs_dir.mkdir(parents=True, exist_ok=True)
    await init_db(shed_settings.db_path)

    app = create_app(shed_settings)
    shedder = app.state.load_shedder
    shedder._inflight = shedder.max_inflight  # noqa: SLF001
    shedder._queued = shedder.max_queue  # noqa: SLF001

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer test-token"},
    ) as c:
        # Trip the shed once.
        r = await c.get("/v1/workspaces")
        assert r.status_code == 503
        # Free the slots so stats can be queried without itself being shed.
        shedder._inflight = 0  # noqa: SLF001
        shedder._queued = 0  # noqa: SLF001

        s = await c.get("/v1/admin/load-shed/stats")
    assert s.status_code == 200
    assert s.json()["shed_count"] == 1


async def test_queue_is_bounded_no_memory_growth() -> None:
    """The queue counter never exceeds ``max_queue`` — memory bound holds."""

    shedder = LoadShedder(max_inflight=1, max_queue=2, enabled=True)
    held: list[asyncio.Future] = []

    async def hold() -> None:
        async with shedder.acquire():
            held.append(asyncio.get_running_loop().create_future())
            await held[-1]

    # Fill inflight slot.
    t_in = asyncio.create_task(hold())
    while shedder.stats["inflight"] < 1:
        await asyncio.sleep(0)

    # Fill queue slots.
    queue_tasks = [asyncio.create_task(hold()) for _ in range(2)]
    while shedder.stats["queued"] < 2:
        await asyncio.sleep(0)

    # Try to enqueue more — must raise.
    rejections = 0
    for _ in range(50):
        try:
            async with shedder.acquire():
                pass
        except OverloadedError:
            rejections += 1
    assert rejections == 50
    assert shedder.stats["queued"] == 2  # never exceeded the cap

    # Release everything.
    for f in held:
        f.set_result(None)
    await asyncio.gather(t_in, *queue_tasks)
