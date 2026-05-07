# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the identity schema migration framework."""

from __future__ import annotations

import contextlib
import io
from pathlib import Path

import aiosqlite
import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from plinth_identity.api import create_app
from plinth_identity.migration_runner import (
    MigrationError,
    MigrationLockError,
    MigrationRunner,
    default_migrations_dir,
    discover_migrations,
    sha256_of_text,
)
from plinth_identity.settings import Settings


MIGRATIONS_DIR = default_migrations_dir(
    str(Path(__file__).resolve().parent.parent / "src" / "plinth_identity" / "__init__.py")
)


@pytest.fixture()
def fresh_db_path(tmp_path: Path) -> Path:
    return tmp_path / "identity.db"


@pytest.fixture()
def runner(fresh_db_path: Path) -> MigrationRunner:
    return MigrationRunner(fresh_db_path, MIGRATIONS_DIR)


# ---------------------------------------------------------------------------
# Discovery


def test_discover_migrations_returns_sorted_list() -> None:
    migrations = discover_migrations(MIGRATIONS_DIR)
    ids = [m.id for m in migrations]
    assert ids == sorted(ids)
    assert ids == ["0001_initial", "0002_signing_keys"]


def test_checksum_changes_with_content() -> None:
    a = sha256_of_text("hello")
    b = sha256_of_text("hello!")
    assert a != b
    assert sha256_of_text("hello") == a


# ---------------------------------------------------------------------------
# Fresh apply


@pytest.mark.asyncio
async def test_apply_pending_on_fresh_db(runner: MigrationRunner) -> None:
    applied = await runner.apply_pending()
    assert [m.id for m in applied] == ["0001_initial", "0002_signing_keys"]
    # Idempotent.
    assert await runner.apply_pending() == []


@pytest.mark.asyncio
async def test_apply_creates_expected_tables(runner: MigrationRunner) -> None:
    await runner.apply_pending()
    async with aiosqlite.connect(runner.db_path) as conn:
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        names = {r[0] for r in await cur.fetchall()}
        await cur.close()
    assert {"issued_tokens", "schema_migrations", "signing_keys", "tenants"} <= names


@pytest.mark.asyncio
async def test_status_after_fresh_apply(runner: MigrationRunner) -> None:
    await runner.apply_pending()
    s = await runner.status()
    assert s.current == "0002_signing_keys"
    assert len(s.applied) == 2
    assert s.pending == []
    assert s.mismatches == []


# ---------------------------------------------------------------------------
# Legacy compatibility


@pytest.mark.asyncio
async def test_legacy_db_marks_migrations_applied(fresh_db_path: Path) -> None:
    """A DB pre-populated by ``init_db`` should mark applied without re-running."""

    from plinth_identity.store import init_db

    await init_db(fresh_db_path)
    runner = MigrationRunner(fresh_db_path, MIGRATIONS_DIR)
    applied = await runner.apply_pending()
    assert len(applied) == 2
    for mig in applied:
        assert mig.duration_ms < 100


@pytest.mark.asyncio
async def test_partial_legacy_db_applies_missing(fresh_db_path: Path) -> None:
    """A DB with only the v0.3 baseline: 0002_signing_keys gets created."""

    fresh_db_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(fresh_db_path) as conn:
        await conn.executescript(
            """
            CREATE TABLE issued_tokens (
              jti TEXT PRIMARY KEY,
              agent_id TEXT NOT NULL,
              tenant_id TEXT NOT NULL,
              workspace_id TEXT,
              scopes TEXT NOT NULL,
              issued_at TIMESTAMP NOT NULL,
              expires_at TIMESTAMP NOT NULL,
              revoked INTEGER NOT NULL DEFAULT 0,
              revoked_at TIMESTAMP,
              metadata TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE tenants (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              metadata TEXT NOT NULL DEFAULT '{}',
              created_at TIMESTAMP NOT NULL
            );
            """
        )
        await conn.commit()
    runner = MigrationRunner(fresh_db_path, MIGRATIONS_DIR)
    await runner.apply_pending()
    s = await runner.status()
    assert s.current == "0002_signing_keys"

    async with aiosqlite.connect(fresh_db_path) as conn:
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        names = {r[0] for r in await cur.fetchall()}
        await cur.close()
    assert "signing_keys" in names


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
        "CREATE TABLE other (id INTEGER PRIMARY KEY);\nNOT VALID;",
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
            ("ffffff", "0001_initial"),
        )
        await conn.commit()
    mm = await runner.verify_checksums()
    assert any(x.id == "0001_initial" for x in mm)


# ---------------------------------------------------------------------------
# apply_to


@pytest.mark.asyncio
async def test_apply_to_partial(runner: MigrationRunner) -> None:
    applied = await runner.apply_to("0001_initial")
    assert [m.id for m in applied] == ["0001_initial"]
    pending = await runner.list_pending()
    assert [m.id for m in pending] == ["0002_signing_keys"]


@pytest.mark.asyncio
async def test_apply_to_unknown_raises(runner: MigrationRunner) -> None:
    with pytest.raises(MigrationError):
        await runner.apply_to("9999_nope")


# ---------------------------------------------------------------------------
# Locking


@pytest.mark.asyncio
async def test_concurrent_apply_409(runner: MigrationRunner) -> None:
    from plinth_identity.migration_runner import _file_lock

    with _file_lock(runner.lock_path, blocking=True):
        with pytest.raises(MigrationLockError):
            await runner.apply_pending(blocking_lock=False)


# ---------------------------------------------------------------------------
# HTTP endpoints


@pytest_asyncio.fixture()
async def app_client(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        identity_jwt_secret="abc" * 16,
        identity_jwt_audience="plinth",
        log_level="WARNING",
        log_format="console",
        auto_migrate=False,
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as c, app.router.lifespan_context(app):
        yield c


@pytest.mark.asyncio
async def test_status_endpoint_shape(app_client: httpx.AsyncClient) -> None:
    resp = await app_client.get("/v1/admin/migrations")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert {"current", "applied", "pending", "mismatches"} <= body.keys()


@pytest.mark.asyncio
async def test_apply_endpoint_runs_pending(app_client: httpx.AsyncClient) -> None:
    pre = (await app_client.get("/v1/admin/migrations")).json()
    initial_pending = len(pre["pending"])
    resp = await app_client.post("/v1/admin/migrations/apply")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert len(body["applied"]) == initial_pending


# ---------------------------------------------------------------------------
# CLI


def test_cli_status_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLINTH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PLINTH_LOG_LEVEL", "WARNING")
    from plinth_identity.__main__ import main

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
    from plinth_identity.__main__ import main

    rc = main(["migrate"])
    assert rc == 0


def test_cli_create_scaffolds_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_dir = tmp_path / "migrations"
    fake_dir.mkdir()
    (fake_dir / "0001_existing.sql").write_text("-- x", encoding="utf-8")
    runner = MigrationRunner(tmp_path / "x.db", fake_dir)
    new = runner.create_migration("rotate stuff")
    assert new.exists()
    assert new.name.startswith("0002_rotate_stuff")


# ---------------------------------------------------------------------------
# Auto-migrate


@pytest.mark.asyncio
async def test_auto_migrate_true_applies_during_lifespan(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        identity_jwt_secret="abc" * 16,
        auto_migrate=True,
        log_level="WARNING",
        log_format="console",
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        runner: MigrationRunner = app.state.migration_runner
        applied = await runner.list_applied()
        assert len(applied) == 2


@pytest.mark.asyncio
async def test_repeated_apply_safe(runner: MigrationRunner) -> None:
    """After applying, drop a row and re-apply — exercise idempotency."""

    await runner.apply_pending()
    async with aiosqlite.connect(runner.db_path) as conn:
        await conn.execute("DELETE FROM schema_migrations WHERE id=?", ("0002_signing_keys",))
        await conn.commit()
    applied = await runner.apply_pending()
    assert [m.id for m in applied] == ["0002_signing_keys"]
