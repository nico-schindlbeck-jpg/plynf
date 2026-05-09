# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the gateway schema migration framework."""

from __future__ import annotations

import contextlib
import io
from pathlib import Path

import aiosqlite
import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from plinth_gateway.api import create_app
from plinth_gateway.migration_runner import (
    MigrationError,
    MigrationLockError,
    MigrationRunner,
    default_migrations_dir,
    discover_migrations,
    sha256_of_text,
)
from plinth_gateway.settings import Settings

# Production migrations dir on disk (services/gateway/migrations).
MIGRATIONS_DIR = default_migrations_dir(
    str(Path(__file__).resolve().parent.parent / "src" / "plinth_gateway" / "__init__.py")
)


@pytest.fixture()
def fresh_db_path(tmp_path: Path) -> Path:
    return tmp_path / "gateway.db"


@pytest.fixture()
def runner(fresh_db_path: Path) -> MigrationRunner:
    return MigrationRunner(fresh_db_path, MIGRATIONS_DIR)


# ---------------------------------------------------------------------------
# Discovery


def test_discover_migrations_returns_sorted_list() -> None:
    migrations = discover_migrations(MIGRATIONS_DIR)
    ids = [m.id for m in migrations]
    assert ids == sorted(ids)
    # The first four are stable; later additions are appended in order.
    assert ids[:4] == [
        "0001_initial",
        "0002_limits",
        "0003_oauth",
        "0004_tenancy",
    ]


def test_checksums_are_deterministic() -> None:
    a = sha256_of_text("hello")
    assert sha256_of_text("hello") == a
    assert sha256_of_text("hello!") != a


# ---------------------------------------------------------------------------
# Fresh apply


@pytest.mark.asyncio
async def test_apply_pending_on_fresh_db(runner: MigrationRunner) -> None:
    applied = await runner.apply_pending()
    ids = [m.id for m in applied]
    # The first four are baseline; later additions are appended in order.
    assert ids[:4] == [
        "0001_initial",
        "0002_limits",
        "0003_oauth",
        "0004_tenancy",
    ]
    assert ids == sorted(ids)
    # Idempotency — second apply is a no-op.
    assert await runner.apply_pending() == []


@pytest.mark.asyncio
async def test_apply_creates_expected_tables(runner: MigrationRunner) -> None:
    await runner.apply_pending()
    async with aiosqlite.connect(runner.db_path) as conn:
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        rows = await cur.fetchall()
        await cur.close()
    names = {r[0] for r in rows}
    for expected in [
        "agent_limits",
        "audit_events",
        "cache_entries",
        "oauth_connections",
        "oauth_states",
        "rate_limit_snapshots",
        "schema_migrations",
        "tools",
    ]:
        assert expected in names


@pytest.mark.asyncio
async def test_status_after_fresh_apply(runner: MigrationRunner) -> None:
    await runner.apply_pending()
    s = await runner.status()
    on_disk = discover_migrations(MIGRATIONS_DIR)
    assert s.current == on_disk[-1].id
    assert len(s.applied) == len(on_disk)
    assert s.pending == []
    assert s.mismatches == []


# ---------------------------------------------------------------------------
# Legacy compatibility


@pytest.mark.asyncio
async def test_legacy_db_marks_migrations_applied(fresh_db_path: Path) -> None:
    """A DB pre-populated by ``Database.connect`` should mark applied without re-running."""

    from plinth_gateway.db import Database

    db = Database(fresh_db_path)
    await db.connect()
    await db.close()

    runner = MigrationRunner(fresh_db_path, MIGRATIONS_DIR)
    applied = await runner.apply_pending()
    on_disk = discover_migrations(MIGRATIONS_DIR)
    assert len(applied) == len(on_disk)
    # Each is fast (heuristic skip).
    for mig in applied:
        assert mig.duration_ms < 100


@pytest.mark.asyncio
async def test_partial_legacy_db_applies_missing(fresh_db_path: Path) -> None:
    """A DB with v0.1 baseline but no v0.2+ tables: missing ones get created.

    We provision the realistic v0.1 schema (timestamp + indices) so the
    later tenancy index in 0004 has all the columns it expects. Stubbing
    only ``id TEXT PRIMARY KEY`` would fail because 0004_tenancy creates
    ``idx_audit_tenant ON audit_events(tenant_id, timestamp DESC)``.
    """

    fresh_db_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(fresh_db_path) as conn:
        await conn.executescript(
            """
            CREATE TABLE tools (
              tool_id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              description TEXT NOT NULL,
              transport TEXT NOT NULL,
              endpoint TEXT NOT NULL,
              input_schema TEXT NOT NULL,
              output_schema TEXT NOT NULL,
              created_at TIMESTAMP NOT NULL,
              updated_at TIMESTAMP NOT NULL
            );
            CREATE TABLE audit_events (
              id TEXT PRIMARY KEY,
              timestamp TIMESTAMP NOT NULL,
              tool_id TEXT NOT NULL,
              workspace_id TEXT,
              agent_id TEXT,
              arguments_hash TEXT NOT NULL,
              result_hash TEXT,
              cached INTEGER NOT NULL DEFAULT 0,
              duration_ms INTEGER NOT NULL,
              cost_estimate_usd REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE cache_entries (
              cache_key TEXT PRIMARY KEY,
              tool_id TEXT NOT NULL,
              arguments_hash TEXT NOT NULL,
              result TEXT NOT NULL,
              created_at TIMESTAMP NOT NULL,
              expires_at TIMESTAMP NOT NULL
            );
            """
        )
        await conn.commit()
    runner = MigrationRunner(fresh_db_path, MIGRATIONS_DIR)
    await runner.apply_pending()
    s = await runner.status()
    on_disk = discover_migrations(MIGRATIONS_DIR)
    assert s.current == on_disk[-1].id

    async with aiosqlite.connect(fresh_db_path) as conn:
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        names = {r[0] for r in await cur.fetchall()}
        await cur.close()
    assert "agent_limits" in names
    assert "oauth_connections" in names


# ---------------------------------------------------------------------------
# Bad SQL rolls back


@pytest.mark.asyncio
async def test_bad_sql_rolls_back(tmp_path: Path) -> None:
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    (mig_dir / "0001_ok.sql").write_text(
        "CREATE TABLE good (id INTEGER PRIMARY KEY);", encoding="utf-8"
    )
    (mig_dir / "0002_broken.sql").write_text(
        "CREATE TABLE other (id INTEGER PRIMARY KEY);\nNOT VALID SQL;",
        encoding="utf-8",
    )
    runner = MigrationRunner(tmp_path / "x.db", mig_dir)
    with pytest.raises(MigrationError):
        await runner.apply_pending()
    async with aiosqlite.connect(runner.db_path) as conn:
        cur = await conn.execute("SELECT id FROM schema_migrations")
        ids = [r[0] for r in await cur.fetchall()]
        await cur.close()
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tables = {r[0] for r in await cur.fetchall()}
        await cur.close()
    assert ids == ["0001_ok"]
    assert "good" in tables
    assert "other" not in tables


# ---------------------------------------------------------------------------
# Checksum mismatch


@pytest.mark.asyncio
async def test_checksum_mismatch_detected(runner: MigrationRunner) -> None:
    await runner.apply_pending()
    async with aiosqlite.connect(runner.db_path) as conn:
        await conn.execute(
            "UPDATE schema_migrations SET checksum=? WHERE id=?",
            ("00bad00", "0003_oauth"),
        )
        await conn.commit()
    mismatches = await runner.verify_checksums()
    assert any(m.id == "0003_oauth" for m in mismatches)


# ---------------------------------------------------------------------------
# apply_to


@pytest.mark.asyncio
async def test_apply_to_partial(runner: MigrationRunner) -> None:
    applied = await runner.apply_to("0002_limits")
    assert [m.id for m in applied] == ["0001_initial", "0002_limits"]
    pending = await runner.list_pending()
    on_disk = discover_migrations(MIGRATIONS_DIR)
    expected = [
        m.id for m in on_disk if m.id not in {"0001_initial", "0002_limits"}
    ]
    assert [m.id for m in pending] == expected


@pytest.mark.asyncio
async def test_apply_to_short_id(runner: MigrationRunner) -> None:
    applied = await runner.apply_to("0001")
    assert [m.id for m in applied] == ["0001_initial"]


@pytest.mark.asyncio
async def test_apply_to_unknown_raises(runner: MigrationRunner) -> None:
    with pytest.raises(MigrationError):
        await runner.apply_to("9999_never")


# ---------------------------------------------------------------------------
# Locking


@pytest.mark.asyncio
async def test_concurrent_apply_409(runner: MigrationRunner) -> None:
    from plinth_gateway.migration_runner import _file_lock

    with _file_lock(runner.lock_path, blocking=True):
        with pytest.raises(MigrationLockError):
            await runner.apply_pending(blocking_lock=False)


# ---------------------------------------------------------------------------
# HTTP endpoints


@pytest_asyncio.fixture()
async def app_client(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        log_level="WARNING",
        log_format="console",
        backend_timeout_seconds=5.0,
        # Keep auth permissive so the admin endpoint accepts the test caller.
        inbound_auth_required=False,
        auto_migrate=False,
    )
    settings.ensure_data_dir()
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as c, app.router.lifespan_context(app):
        yield c


@pytest.mark.asyncio
async def test_status_endpoint_shape(app_client: httpx.AsyncClient) -> None:
    resp = await app_client.get("/v1/admin/migrations")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert {"current", "applied", "pending", "mismatches"} <= body.keys()
    for entry in body["applied"]:
        assert {"id", "checksum", "applied_at", "duration_ms"} <= entry.keys()


@pytest.mark.asyncio
async def test_apply_endpoint_runs_pending(app_client: httpx.AsyncClient) -> None:
    pre = (await app_client.get("/v1/admin/migrations")).json()
    # auto_migrate=False → all four pending (DB.connect creates them; runner
    # detects "already applied" and marks them — so the count of pending is 0
    # already).
    initial_pending = len(pre["pending"])

    resp = await app_client.post("/v1/admin/migrations/apply")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert isinstance(body["applied"], list)
    # If pending was non-zero, all became applied.
    assert len(body["applied"]) == initial_pending


# ---------------------------------------------------------------------------
# CLI


def test_cli_status_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLINTH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PLINTH_LOG_LEVEL", "WARNING")
    from plinth_gateway.__main__ import main

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main(["migrate", "--status"])
    out = buf.getvalue()
    assert rc == 0
    assert "current:" in out
    assert "applied:" in out
    assert "pending:" in out


def test_cli_apply_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLINTH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PLINTH_LOG_LEVEL", "WARNING")
    from plinth_gateway.__main__ import main

    rc = main(["migrate"])
    assert rc == 0


def test_cli_create_scaffolds_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLINTH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PLINTH_LOG_LEVEL", "WARNING")
    fake_dir = tmp_path / "migrations"
    fake_dir.mkdir()
    (fake_dir / "0001_seed.sql").write_text("-- seed", encoding="utf-8")
    runner = MigrationRunner(tmp_path / "x.db", fake_dir)
    new = runner.create_migration("add stuff")
    assert new.exists()
    assert new.name.startswith("0002_add_stuff")


# ---------------------------------------------------------------------------
# Auto-migrate flag


@pytest.mark.asyncio
async def test_auto_migrate_true_applies_during_lifespan(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        auto_migrate=True,
        log_level="WARNING",
        log_format="console",
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        runner: MigrationRunner = app.state.migration_runner
        applied = await runner.list_applied()
        on_disk = discover_migrations(MIGRATIONS_DIR)
        assert len(applied) == len(on_disk)


@pytest.mark.asyncio
async def test_repeated_apply_is_safe(runner: MigrationRunner) -> None:
    """Re-applying ``0004_tenancy`` after deleting the row exercises the
    duplicate-column-name swallow path."""

    await runner.apply_pending()
    async with aiosqlite.connect(runner.db_path) as conn:
        await conn.execute("DELETE FROM schema_migrations WHERE id=?", ("0004_tenancy",))
        await conn.commit()
    applied = await runner.apply_pending()
    assert [m.id for m in applied] == ["0004_tenancy"]


# ---------------------------------------------------------------------------
# v0.6 — Postgres advisory lock dispatcher


def test_advisory_lock_id_is_deterministic(tmp_path: Path) -> None:
    a = MigrationRunner(
        tmp_path / "a.db", MIGRATIONS_DIR, service_name="gateway"
    )
    b = MigrationRunner(
        tmp_path / "b.db", MIGRATIONS_DIR, service_name="gateway"
    )
    assert a._compute_lock_id() == b._compute_lock_id()
    assert a._compute_lock_id() >= 0


def test_advisory_lock_id_differs_per_service(tmp_path: Path) -> None:
    workspace_runner = MigrationRunner(
        tmp_path / "ws.db", MIGRATIONS_DIR, service_name="workspace"
    )
    gateway_runner = MigrationRunner(
        tmp_path / "gw.db", MIGRATIONS_DIR, service_name="gateway"
    )
    identity_runner = MigrationRunner(
        tmp_path / "id.db", MIGRATIONS_DIR, service_name="identity"
    )
    ids = {
        workspace_runner._compute_lock_id(),
        gateway_runner._compute_lock_id(),
        identity_runner._compute_lock_id(),
    }
    assert len(ids) == 3


def test_is_postgres_detects_dsns(tmp_path: Path) -> None:
    for url in [
        "postgres://u:p@h/db",
        "postgresql://u:p@h/db",
        "postgresql+asyncpg://u:p@h/db",
        "POSTGRESQL://u:p@h/db",
    ]:
        runner = MigrationRunner(
            tmp_path / "x.db", MIGRATIONS_DIR, database_url=url
        )
        assert runner._is_postgres() is True, url
    runner = MigrationRunner(tmp_path / "x.db", MIGRATIONS_DIR)
    assert runner._is_postgres() is False


@pytest.mark.asyncio
async def test_acquire_lock_uses_sqlite_path_by_default(
    tmp_path: Path,
) -> None:
    runner = MigrationRunner(
        tmp_path / "gw.db",
        MIGRATIONS_DIR,
        lock_path=tmp_path / ".migration.lock",
    )
    async with runner._acquire_lock():
        assert (tmp_path / ".migration.lock").exists()


@pytest.mark.asyncio
async def test_acquire_lock_dispatches_to_postgres_when_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import contextlib as _contextlib

    runner = MigrationRunner(
        tmp_path / "gw.db",
        MIGRATIONS_DIR,
        database_url="postgresql://stub/db",
    )
    called = {"pg": 0, "file": 0}

    @_contextlib.asynccontextmanager
    async def fake_pg(self, *, blocking: bool = True):
        called["pg"] += 1
        yield

    @_contextlib.contextmanager
    def fake_file(*args, **kwargs):
        called["file"] += 1
        yield

    monkeypatch.setattr(
        type(runner), "_pg_advisory_lock", fake_pg, raising=True
    )
    from plinth_gateway import migration_runner as mr_module

    monkeypatch.setattr(mr_module, "_file_lock", fake_file)

    async with runner._acquire_lock():
        pass

    assert called["pg"] == 1
    assert called["file"] == 0


@pytest.mark.asyncio
async def test_advisory_lock_against_live_postgres(tmp_path: Path) -> None:
    """Skipped unless PLINTH_TEST_POSTGRES_URL set."""

    import os

    pg_url = os.environ.get("PLINTH_TEST_POSTGRES_URL")
    if not pg_url:
        pytest.skip("PLINTH_TEST_POSTGRES_URL not set; skipping live PG test")

    runner = MigrationRunner(
        tmp_path / "ignored.db",
        MIGRATIONS_DIR,
        database_url=pg_url,
        service_name="gateway",
    )
    async with runner._acquire_lock():
        pass
