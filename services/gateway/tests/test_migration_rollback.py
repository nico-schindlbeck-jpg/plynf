# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the v0.6 migration-rollback feature in the gateway service.

The runner-level apply path is exercised by ``test_migrations.py``; this
module focuses on the new rollback execution surface (CLI flag, HTTP
endpoint, atomicity guarantees, checksum verification).

Each test sets up a temporary migrations directory or uses the production
one so the assertions stay valid even as new gateway migrations land.
"""

from __future__ import annotations

import contextlib
import io
import sqlite3
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
    MigrationNotFound,
    MigrationRollbackMissing,
    MigrationRunner,
    RolledBackMigration,
    default_migrations_dir,
)
from plinth_gateway.settings import Settings

MIGRATIONS_DIR = default_migrations_dir(
    str(
        Path(__file__).resolve().parent.parent
        / "src"
        / "plinth_gateway"
        / "__init__.py"
    )
)


# ---------------------------------------------------------------------------
# Fixtures


@pytest.fixture()
def runner(tmp_path: Path) -> MigrationRunner:
    """Runner pointed at the production migrations dir, fresh DB each test."""

    return MigrationRunner(tmp_path / "gateway.db", MIGRATIONS_DIR)


@pytest_asyncio.fixture()
async def app_client(tmp_path: Path):
    """FastAPI client wired to the gateway app with auto_migrate=False."""

    settings = Settings(
        data_dir=tmp_path,
        log_level="WARNING",
        log_format="console",
        backend_timeout_seconds=5.0,
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


# ---------------------------------------------------------------------------
# Runner-level rollback


@pytest.mark.asyncio
async def test_rollback_target_not_found(runner: MigrationRunner) -> None:
    """A target that doesn't exist on disk raises ``MigrationNotFound``."""

    await runner.apply_pending()
    with pytest.raises(MigrationNotFound):
        await runner.rollback_to("9999_nonexistent")


@pytest.mark.asyncio
async def test_rollback_target_not_applied(tmp_path: Path) -> None:
    """A target that exists on disk but isn't applied raises ``MigrationError``.

    Set up a custom migrations dir so we can apply only a prefix of the set
    and try to roll back to the not-yet-applied tail.
    """

    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    (mig_dir / "0001_a.sql").write_text(
        "CREATE TABLE a (id INTEGER PRIMARY KEY);", encoding="utf-8"
    )
    (mig_dir / "0001_a_rollback.sql").write_text(
        "DROP TABLE IF EXISTS a;", encoding="utf-8"
    )
    (mig_dir / "0002_b.sql").write_text(
        "CREATE TABLE b (id INTEGER PRIMARY KEY);", encoding="utf-8"
    )
    (mig_dir / "0002_b_rollback.sql").write_text(
        "DROP TABLE IF EXISTS b;", encoding="utf-8"
    )

    runner = MigrationRunner(tmp_path / "x.db", mig_dir)
    # Apply only 0001.
    await runner.apply_to("0001_a")

    # 0002 exists on disk but isn't applied, so rolling back to it must fail.
    with pytest.raises(MigrationError) as exc_info:
        await runner.rollback_to("0002_b")
    assert "not currently applied" in str(exc_info.value)


@pytest.mark.asyncio
async def test_rollback_to_target_succeeds(tmp_path: Path) -> None:
    """Apply 5, rollback to 3, only 3 remain.

    Uses a synthetic migrations directory so the test stays robust against
    the production gateway migration set growing.
    """

    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    for idx, name in enumerate(["a", "b", "c", "d", "e"], start=1):
        (mig_dir / f"{idx:04d}_{name}.sql").write_text(
            f"CREATE TABLE {name} (id INTEGER PRIMARY KEY);",
            encoding="utf-8",
        )
        (mig_dir / f"{idx:04d}_{name}_rollback.sql").write_text(
            f"DROP TABLE IF EXISTS {name};", encoding="utf-8"
        )

    runner = MigrationRunner(tmp_path / "x.db", mig_dir)
    await runner.apply_pending()

    outcome = await runner.rollback_to("0003_c")
    assert outcome.failed is None
    rolled_ids = [entry.id for entry in outcome.rolled_back]
    assert rolled_ids == ["0005_e", "0004_d"]
    # All entries are RolledBackMigration objects with timing.
    for entry in outcome.rolled_back:
        assert isinstance(entry, RolledBackMigration)
        assert entry.duration_ms >= 0

    async with aiosqlite.connect(runner.db_path) as conn:
        cur = await conn.execute(
            "SELECT id FROM schema_migrations ORDER BY id"
        )
        ids = [r[0] for r in await cur.fetchall()]
        await cur.close()
    assert ids == ["0001_a", "0002_b", "0003_c"]


@pytest.mark.asyncio
async def test_rollback_missing_file_fails_atomically(
    tmp_path: Path,
) -> None:
    """If any rollback file is missing, no rollbacks are applied at all.

    This is the spec's "atomicity of the plan" guarantee: a missing file
    in the *middle* of the plan must abort *before* touching anything.
    """

    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    (mig_dir / "0001_a.sql").write_text(
        "CREATE TABLE a (id INTEGER PRIMARY KEY);", encoding="utf-8"
    )
    (mig_dir / "0001_a_rollback.sql").write_text(
        "DROP TABLE IF EXISTS a;", encoding="utf-8"
    )
    (mig_dir / "0002_b.sql").write_text(
        "CREATE TABLE b (id INTEGER PRIMARY KEY);", encoding="utf-8"
    )
    # Note: 0002 has NO rollback file.
    (mig_dir / "0003_c.sql").write_text(
        "CREATE TABLE c (id INTEGER PRIMARY KEY);", encoding="utf-8"
    )
    (mig_dir / "0003_c_rollback.sql").write_text(
        "DROP TABLE IF EXISTS c;", encoding="utf-8"
    )

    runner = MigrationRunner(tmp_path / "x.db", mig_dir)
    await runner.apply_pending()

    with pytest.raises(MigrationRollbackMissing) as exc_info:
        await runner.rollback_to("0001_a")
    assert "0002_b" in exc_info.value.missing_ids

    # Database untouched: all three rows still in schema_migrations,
    # all three tables still present.
    async with aiosqlite.connect(runner.db_path) as conn:
        cur = await conn.execute(
            "SELECT id FROM schema_migrations ORDER BY id"
        )
        ids = [r[0] for r in await cur.fetchall()]
        await cur.close()
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tables = {r[0] for r in await cur.fetchall()}
        await cur.close()
    assert ids == ["0001_a", "0002_b", "0003_c"]
    assert {"a", "b", "c"} <= tables


@pytest.mark.asyncio
async def test_rollback_dry_run_does_not_mutate(tmp_path: Path) -> None:
    """Dry-run returns the plan without executing anything."""

    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    for idx, name in enumerate(["a", "b", "c"], start=1):
        (mig_dir / f"{idx:04d}_{name}.sql").write_text(
            f"CREATE TABLE {name} (id INTEGER PRIMARY KEY);",
            encoding="utf-8",
        )
        (mig_dir / f"{idx:04d}_{name}_rollback.sql").write_text(
            f"DROP TABLE IF EXISTS {name};", encoding="utf-8"
        )

    runner = MigrationRunner(tmp_path / "x.db", mig_dir)
    await runner.apply_pending()

    # Snapshot the row count + table count before the dry run.
    async with aiosqlite.connect(runner.db_path) as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM schema_migrations")
        pre_count = (await cur.fetchone())[0]
        await cur.close()
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        pre_tables = {r[0] for r in await cur.fetchall()}
        await cur.close()

    outcome = await runner.rollback_to("0001_a", dry_run=True)
    assert outcome.dry_run is True
    rolled_ids = [entry.id for entry in outcome.rolled_back]
    assert rolled_ids == ["0003_c", "0002_b"]

    # Database unchanged.
    async with aiosqlite.connect(runner.db_path) as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM schema_migrations")
        post_count = (await cur.fetchone())[0]
        await cur.close()
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        post_tables = {r[0] for r in await cur.fetchall()}
        await cur.close()
    assert post_count == pre_count
    assert post_tables == pre_tables


@pytest.mark.asyncio
async def test_rollback_checksum_mismatch_detected(
    tmp_path: Path,
) -> None:
    """Tampering a rollback file *in the plan* triggers checksum error.

    Apply forward (recording the rollback checksum), tamper the rollback
    file content, then attempt the rollback. The runner must refuse
    because the on-disk content has drifted from what was recorded at
    apply time — a defence against tampered rollback SQL silently
    mutating the database.
    """

    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    (mig_dir / "0001_a.sql").write_text(
        "CREATE TABLE a (id INTEGER PRIMARY KEY);", encoding="utf-8"
    )
    (mig_dir / "0001_a_rollback.sql").write_text(
        "DROP TABLE IF EXISTS a;", encoding="utf-8"
    )
    (mig_dir / "0002_b.sql").write_text(
        "CREATE TABLE b (id INTEGER PRIMARY KEY);", encoding="utf-8"
    )
    rb2_path = mig_dir / "0002_b_rollback.sql"
    rb2_path.write_text("DROP TABLE IF EXISTS b;", encoding="utf-8")

    runner = MigrationRunner(tmp_path / "x.db", mig_dir)
    await runner.apply_pending()

    # Tamper with 0002's rollback file (it's in the rollback plan when
    # rolling back to 0001).
    rb2_path.write_text(
        "-- TAMPERED\nDROP TABLE IF EXISTS b;",
        encoding="utf-8",
    )

    runner2 = MigrationRunner(runner.db_path, mig_dir)
    with pytest.raises(MigrationError) as exc_info:
        await runner2.rollback_to("0001_a")
    assert "checksum mismatch" in str(exc_info.value)
    assert "0002_b" in str(exc_info.value)


@pytest.mark.asyncio
async def test_list_rollback_targets_excludes_first(tmp_path: Path) -> None:
    """``list_rollback_targets`` excludes the very first applied migration."""

    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    for idx, name in enumerate(["a", "b", "c"], start=1):
        (mig_dir / f"{idx:04d}_{name}.sql").write_text(
            f"CREATE TABLE {name} (id INTEGER PRIMARY KEY);",
            encoding="utf-8",
        )
        (mig_dir / f"{idx:04d}_{name}_rollback.sql").write_text(
            f"DROP TABLE IF EXISTS {name};", encoding="utf-8"
        )

    runner = MigrationRunner(tmp_path / "x.db", mig_dir)
    await runner.apply_pending()
    targets = await runner.list_rollback_targets()
    # The very first migration is excluded — rolling back through it would
    # leave the schema empty, which the spec explicitly disallows.
    assert "0001_a" not in targets
    assert "0002_b" in targets
    assert "0003_c" in targets


# ---------------------------------------------------------------------------
# CLI


def test_cli_rollback_to_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``migrate --rollback-to <id>`` rolls back through the CLI."""

    monkeypatch.setenv("PLINTH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PLINTH_LOG_LEVEL", "WARNING")
    from plinth_gateway.__main__ import main

    assert main(["migrate"]) == 0

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main(["migrate", "--rollback-to", "0003_oauth"])
    assert rc == 0
    out = buf.getvalue()
    assert "Rolling back" in out
    assert "0004_tenancy" in out
    assert "rolled back" in out
    assert "Done." in out


def test_cli_dry_run_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--rollback-to ... --dry-run`` prints the plan without mutating."""

    monkeypatch.setenv("PLINTH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PLINTH_LOG_LEVEL", "WARNING")
    from plinth_gateway.__main__ import main

    main(["migrate"])

    db_path = tmp_path / "gateway.db"
    with sqlite3.connect(db_path) as conn:
        pre = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0]

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main(
            ["migrate", "--rollback-to", "0003_oauth", "--dry-run"]
        )
    out = buf.getvalue()
    assert rc == 0
    assert "[DRY-RUN]" in out
    assert "0004_tenancy" in out
    assert "No SQL was executed" in out

    with sqlite3.connect(db_path) as conn:
        post = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0]
    assert post == pre


# ---------------------------------------------------------------------------
# HTTP endpoint


@pytest.mark.asyncio
async def test_endpoint_rollback(app_client: httpx.AsyncClient) -> None:
    """POST /v1/admin/migrations/rollback executes a rollback end-to-end."""

    await app_client.post("/v1/admin/migrations/apply")

    resp = await app_client.post(
        "/v1/admin/migrations/rollback",
        json={"to": "0003_oauth", "dry_run": False},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["target"] == "0003_oauth"
    rolled_ids = [entry["id"] for entry in body["rolled_back"]]
    assert "0004_tenancy" in rolled_ids
    for entry in body["rolled_back"]:
        # Per RolledBackMigration spec.
        assert "id" in entry
        assert "rolled_back_at" in entry
        assert "duration_ms" in entry
    assert body["failed"] is None
    assert body["dry_run"] is False


@pytest.mark.asyncio
async def test_endpoint_rollback_dry_run(
    app_client: httpx.AsyncClient,
) -> None:
    """Dry-run endpoint returns the plan without mutating."""

    await app_client.post("/v1/admin/migrations/apply")
    pre = (await app_client.get("/v1/admin/migrations")).json()
    pre_pending = {p["id"] for p in pre["pending"]}

    resp = await app_client.post(
        "/v1/admin/migrations/rollback",
        json={"to": "0003_oauth", "dry_run": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dry_run"] is True

    # Pending set unchanged after dry-run.
    post = (await app_client.get("/v1/admin/migrations")).json()
    post_pending = {p["id"] for p in post["pending"]}
    assert post_pending == pre_pending


@pytest.mark.asyncio
async def test_endpoint_rollback_locked_returns_409(
    app_client: httpx.AsyncClient,
    tmp_path: Path,
) -> None:
    """Concurrent rollback attempts surface as 409 ``MIGRATION_LOCKED``."""

    await app_client.post("/v1/admin/migrations/apply")

    # Hold the file lock manually, then expect 409 from the endpoint.
    runner: MigrationRunner = (
        app_client._transport.app.state.migration_runner  # type: ignore[attr-defined]
    )
    from plinth_gateway.migration_runner import _file_lock as _fl

    with _fl(runner.lock_path, blocking=True):
        resp = await app_client.post(
            "/v1/admin/migrations/rollback",
            json={"to": "0003_oauth", "dry_run": False},
        )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "MIGRATION_LOCKED"


@pytest.mark.asyncio
async def test_endpoint_rollback_missing_file_returns_400(
    app_client: httpx.AsyncClient,
) -> None:
    """Rolling through a migration that lacks a rollback file returns 400."""

    await app_client.post("/v1/admin/migrations/apply")

    # 0001_initial → 0002_limits has no rollback file shipped, so trying
    # to roll back to 0001_initial requires it and must fail.
    resp = await app_client.post(
        "/v1/admin/migrations/rollback",
        json={"to": "0001_initial", "dry_run": False},
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["error"]["code"] == "MIGRATION_ROLLBACK_MISSING"
    missing = body["error"]["details"].get("missing_ids", [])
    assert "0002_limits" in missing


# Catch the unused-import warning — ``MigrationLockError`` is exported so
# downstream callers can import it from this module's typing perspective.
_ = MigrationLockError
