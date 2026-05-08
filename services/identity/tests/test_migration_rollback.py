# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the v0.6 migration-rollback feature in the identity service.

The runner-level apply path is exercised by ``test_migrations.py``; this
module focuses on the new rollback execution surface (CLI flag, HTTP
endpoint, atomicity guarantees, checksum verification).
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

from plinth_identity.api import create_app
from plinth_identity.migration_runner import (
    MigrationError,
    MigrationLockError,
    MigrationNotFound,
    MigrationRollbackMissing,
    MigrationRunner,
    RolledBackMigration,
    default_migrations_dir,
)
from plinth_identity.settings import Settings

MIGRATIONS_DIR = default_migrations_dir(
    str(
        Path(__file__).resolve().parent.parent
        / "src"
        / "plinth_identity"
        / "__init__.py"
    )
)


# ---------------------------------------------------------------------------
# Fixtures


@pytest.fixture()
def runner(tmp_path: Path) -> MigrationRunner:
    """Runner pointed at the production migrations dir, fresh DB each test."""

    return MigrationRunner(tmp_path / "identity.db", MIGRATIONS_DIR)


@pytest_asyncio.fixture()
async def app_client(tmp_path: Path):
    """FastAPI client wired to the identity app with auto_migrate=False."""

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
    """A target that exists on disk but isn't applied raises ``MigrationError``."""

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
    await runner.apply_to("0001_a")

    with pytest.raises(MigrationError) as exc_info:
        await runner.rollback_to("0002_b")
    assert "not currently applied" in str(exc_info.value)


@pytest.mark.asyncio
async def test_rollback_to_target_succeeds(tmp_path: Path) -> None:
    """Apply 5, rollback to 3, only 3 remain."""

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
    """Missing rollback file in the plan halts before any rollback runs."""

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
    # No 0002_b_rollback.sql.
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

    # Database untouched.
    async with aiosqlite.connect(runner.db_path) as conn:
        cur = await conn.execute(
            "SELECT id FROM schema_migrations ORDER BY id"
        )
        ids = [r[0] for r in await cur.fetchall()]
        await cur.close()
    assert ids == ["0001_a", "0002_b", "0003_c"]


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

    async with aiosqlite.connect(runner.db_path) as conn:
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        pre_tables = {r[0] for r in await cur.fetchall()}
        await cur.close()

    outcome = await runner.rollback_to("0001_a", dry_run=True)
    assert outcome.dry_run is True
    rolled_ids = [entry.id for entry in outcome.rolled_back]
    assert rolled_ids == ["0003_c", "0002_b"]

    async with aiosqlite.connect(runner.db_path) as conn:
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        post_tables = {r[0] for r in await cur.fetchall()}
        await cur.close()
    assert post_tables == pre_tables


@pytest.mark.asyncio
async def test_rollback_checksum_mismatch_detected(
    tmp_path: Path,
) -> None:
    """Tampering a rollback file in the plan triggers checksum error."""

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
    rb2 = mig_dir / "0002_b_rollback.sql"
    rb2.write_text("DROP TABLE IF EXISTS b;", encoding="utf-8")

    runner = MigrationRunner(tmp_path / "x.db", mig_dir)
    await runner.apply_pending()

    rb2.write_text(
        "-- TAMPERED\nDROP TABLE IF EXISTS b;",
        encoding="utf-8",
    )

    runner2 = MigrationRunner(runner.db_path, mig_dir)
    with pytest.raises(MigrationError) as exc_info:
        await runner2.rollback_to("0001_a")
    assert "checksum mismatch" in str(exc_info.value)
    assert "0002_b" in str(exc_info.value)


@pytest.mark.asyncio
async def test_list_rollback_targets_excludes_first(
    tmp_path: Path,
) -> None:
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
    monkeypatch.setenv("PLINTH_IDENTITY_JWT_SECRET", "abc" * 16)
    from plinth_identity.__main__ import main

    assert main(["migrate"]) == 0

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main(["migrate", "--rollback-to", "0001_initial"])
    assert rc == 0
    out = buf.getvalue()
    assert "Rolling back" in out
    assert "0002_signing_keys" in out
    assert "rolled back" in out
    assert "Done." in out


def test_cli_dry_run_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--rollback-to ... --dry-run`` prints the plan without mutating."""

    monkeypatch.setenv("PLINTH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PLINTH_LOG_LEVEL", "WARNING")
    monkeypatch.setenv("PLINTH_IDENTITY_JWT_SECRET", "abc" * 16)
    from plinth_identity.__main__ import main

    main(["migrate"])

    db_path = tmp_path / "identity.db"
    with sqlite3.connect(db_path) as conn:
        pre = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0]

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main(
            [
                "migrate",
                "--rollback-to",
                "0001_initial",
                "--dry-run",
            ]
        )
    out = buf.getvalue()
    assert rc == 0
    assert "[DRY-RUN]" in out
    assert "0002_signing_keys" in out
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
        json={"to": "0001_initial", "dry_run": False},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["target"] == "0001_initial"
    rolled_ids = [entry["id"] for entry in body["rolled_back"]]
    assert "0002_signing_keys" in rolled_ids
    for entry in body["rolled_back"]:
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
        json={"to": "0001_initial", "dry_run": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dry_run"] is True

    post = (await app_client.get("/v1/admin/migrations")).json()
    post_pending = {p["id"] for p in post["pending"]}
    assert post_pending == pre_pending


# Catch the unused-import warning — kept for downstream typing imports.
_ = MigrationLockError
