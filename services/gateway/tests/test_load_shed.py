# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Load-shedding middleware tests for the gateway service.

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
from httpx import ASGITransport, AsyncClient

from plinth_gateway.api import create_app
from plinth_gateway.load_shed import LoadShedder, OverloadedError
from plinth_gateway.settings import Settings


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
    held: list[asyncio.Future] = []

    async def hold() -> None:
        async with shedder.acquire():
            held.append(asyncio.get_running_loop().create_future())
            await held[-1]

    t1 = asyncio.create_task(hold())
    t2 = asyncio.create_task(hold())
    while shedder.stats["inflight"] < 2:
        await asyncio.sleep(0)

    async with shedder.acquire():
        assert shedder.stats["queued"] == 1
    assert shedder.stats["queued"] == 0

    for f in held:
        f.set_result(None)
    await asyncio.gather(t1, t2)


async def test_shedder_rejects_when_full_with_retry_hint() -> None:
    """Over capacity → ``OverloadedError`` carrying retry hint."""

    shedder = LoadShedder(
        max_inflight=1,
        max_queue=1,
        retry_after_seconds=5,
        enabled=True,
    )
    held: asyncio.Future
    qheld: asyncio.Future

    async def hold_inflight() -> None:
        nonlocal held
        async with shedder.acquire():
            held = asyncio.get_running_loop().create_future()
            await held

    async def hold_queue() -> None:
        nonlocal qheld
        async with shedder.acquire():
            qheld = asyncio.get_running_loop().create_future()
            await qheld

    t_in = asyncio.create_task(hold_inflight())
    while shedder.stats["inflight"] < 1:
        await asyncio.sleep(0)
    t_q = asyncio.create_task(hold_queue())
    while shedder.stats["queued"] < 1:
        await asyncio.sleep(0)

    with pytest.raises(OverloadedError) as exc:
        async with shedder.acquire():
            pytest.fail()
    assert exc.value.retry_after == 5
    assert shedder.stats["shed_count"] == 1

    held.set_result(None)
    qheld.set_result(None)
    await asyncio.gather(t_in, t_q)


# ---------------------------------------------------------------------------
# Integration: middleware against the FastAPI app
# ---------------------------------------------------------------------------


@pytest.fixture
def shed_settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        gateway_host="127.0.0.1",
        gateway_port=7422,
        log_level="WARNING",
        log_format="console",
        backend_timeout_seconds=5.0,
        inbound_auth_required=False,
        load_shed_enabled=True,
        load_shed_max_inflight=2,
        load_shed_max_queue=1,
        load_shed_retry_after_seconds=3,
    )


@pytest_asyncio.fixture
async def shed_client(shed_settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_app(shed_settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": "Bearer test-token"},
    ) as c:
        async with app.router.lifespan_context(app):
            yield c


async def test_disabled_no_shedding(client: AsyncClient) -> None:
    """Default settings (shed disabled) → every request lands."""

    coros = [client.get("/healthz") for _ in range(20)]
    coros += [client.get("/v1/tools") for _ in range(20)]
    results = await asyncio.gather(*coros)
    assert all(r.status_code == 200 for r in results)


async def test_enabled_below_threshold_passes(shed_client: AsyncClient) -> None:
    """Enabled but lightly loaded → no 503s."""

    r1 = await shed_client.get("/healthz")
    r2 = await shed_client.get("/v1/tools")
    assert r1.status_code == 200
    assert r2.status_code == 200


async def test_healthz_always_passes_under_shed(
    shed_settings: Settings,
) -> None:
    """``/healthz`` is exempt — even when the shedder is at capacity."""

    app = create_app(shed_settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": "Bearer test-token"},
    ) as c:
        async with app.router.lifespan_context(app):
            shedder = app.state.load_shedder
            shedder._inflight = shedder.max_inflight  # noqa: SLF001
            shedder._queued = shedder.max_queue  # noqa: SLF001

            r = await c.get("/healthz")
            assert r.status_code == 200

            with pytest.raises(OverloadedError):
                async with shedder.acquire():
                    pytest.fail()


async def test_overload_returns_503_with_retry_after(
    shed_settings: Settings,
) -> None:
    """A request that hits the shed limit gets 503 + ``Retry-After``."""

    app = create_app(shed_settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": "Bearer test-token"},
    ) as c:
        async with app.router.lifespan_context(app):
            shedder = app.state.load_shedder
            shedder._inflight = shedder.max_inflight  # noqa: SLF001
            shedder._queued = shedder.max_queue  # noqa: SLF001

            r = await c.get("/v1/tools")
        assert r.status_code == 503
        assert r.headers["Retry-After"] == "3"
        body = r.json()
        assert body["error"]["code"] == "OVERLOADED"
        assert body["error"]["details"]["retry_after_seconds"] == 3
        assert shedder.stats["shed_count"] == 1


async def test_admin_stats_endpoint(shed_client: AsyncClient) -> None:
    """``/v1/admin/load-shed/stats`` returns the counters."""

    r = await shed_client.get("/v1/admin/load-shed/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["max_inflight"] == 2
    assert body["max_queue"] == 1
    assert body["enabled"] is True
    assert "shed_count" in body
    assert "inflight" in body
    assert "queued" in body


async def test_admin_stats_reports_shed_count(
    shed_settings: Settings,
) -> None:
    """The shed_count increments after a 503."""

    app = create_app(shed_settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": "Bearer test-token"},
    ) as c:
        async with app.router.lifespan_context(app):
            shedder = app.state.load_shedder
            shedder._inflight = shedder.max_inflight  # noqa: SLF001
            shedder._queued = shedder.max_queue  # noqa: SLF001

            r = await c.get("/v1/tools")
            assert r.status_code == 503

            shedder._inflight = 0  # noqa: SLF001
            shedder._queued = 0  # noqa: SLF001
            s = await c.get("/v1/admin/load-shed/stats")
        assert s.status_code == 200
        assert s.json()["shed_count"] == 1


async def test_queue_is_bounded_no_memory_growth() -> None:
    """The queue counter never exceeds ``max_queue``."""

    shedder = LoadShedder(max_inflight=1, max_queue=2, enabled=True)
    held: list[asyncio.Future] = []

    async def hold() -> None:
        async with shedder.acquire():
            held.append(asyncio.get_running_loop().create_future())
            await held[-1]

    t_in = asyncio.create_task(hold())
    while shedder.stats["inflight"] < 1:
        await asyncio.sleep(0)

    queue_tasks = [asyncio.create_task(hold()) for _ in range(2)]
    while shedder.stats["queued"] < 2:
        await asyncio.sleep(0)

    rejections = 0
    for _ in range(50):
        try:
            async with shedder.acquire():
                pass
        except OverloadedError:
            rejections += 1
    assert rejections == 50
    assert shedder.stats["queued"] == 2

    for f in held:
        f.set_result(None)
    await asyncio.gather(t_in, *queue_tasks)
