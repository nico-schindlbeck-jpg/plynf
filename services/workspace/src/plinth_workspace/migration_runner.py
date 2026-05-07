# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Schema migration runner for the workspace service.

The runner reads forward migration files from ``migrations/``, applies any
that haven't been recorded in ``schema_migrations``, and tracks each
application's checksum and duration.

Design constraints
------------------

1. **Atomicity per file** — Each migration runs in its own transaction.
   A SQL error rolls back the file fully; no half-state.
2. **Locking** — A file-system lock at ``$DATA_DIR/.migration.lock`` keeps
   concurrent processes serialised. Acquired with ``fcntl.flock``; released
   on context exit. The Postgres equivalent (``pg_advisory_lock``) is not
   wired in v0.5 because the workspace service uses SQLite locally.
3. **Backwards compatibility** — On first run against an existing
   v0.1–v0.4 database, the runner inspects each pending migration: if
   every CREATE TABLE statement targets a table that already exists, the
   migration is recorded as applied without executing. This lets databases
   provisioned by the legacy CREATE-IF-NOT-EXISTS bootstrap upgrade
   cleanly.
4. **Checksums** — Each applied migration records ``sha256(file.read_bytes())``.
   :meth:`verify_checksums` compares the stored value against a fresh hash;
   a mismatch means someone edited history.

Migration file format
---------------------

A migration is a plain ``.sql`` file. The filename's prefix (everything up
to the first ``.sql``) is the migration ID — e.g. ``0003_workflows.sql``
becomes ID ``0003_workflows``. IDs sort lexically in application order, so
zero-pad the numeric prefix.

Sibling rollback files (``<id>_rollback.sql``) are documentary in v0.5;
automated rollback ships in v0.6.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional  # noqa: UP035

import aiosqlite

UTC = timezone.utc  # noqa: UP017

# Filenames matching this pattern are treated as migration files (excludes
# README.md, rollback files, and editor backup files).
_MIGRATION_FILE_RE = re.compile(r"^(?P<id>\d+_[A-Za-z0-9_-]+)\.sql$")
_ROLLBACK_FILE_RE = re.compile(r"^(?P<id>\d+_[A-Za-z0-9_-]+)_rollback\.sql$")

# Names of CREATE TABLE statements — used by the "already-applied" detector
# to decide whether a pending migration is a no-op against a legacy DB.
_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)",
    re.IGNORECASE,
)
_ALTER_TABLE_ADD_COL_RE = re.compile(
    r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Migration:
    """A migration file on disk."""

    id: str
    path: Path
    sql: str
    checksum: str


@dataclass(frozen=True)
class AppliedMigration:
    """A migration recorded in ``schema_migrations``.

    Carries the same fields as :class:`Migration` plus the application
    metadata. Kept as a separate dataclass so callers can pattern-match on
    "applied vs pending" without checking optional fields.
    """

    id: str
    path: Path
    sql: str
    checksum: str
    applied_at: datetime
    duration_ms: int


@dataclass(frozen=True)
class ChecksumMismatch:
    """An applied migration whose on-disk content has drifted from record."""

    id: str
    stored_checksum: str
    current_checksum: str


@dataclass(frozen=True)
class MigrationStatus:
    """Snapshot of applied + pending migrations."""

    applied: list[AppliedMigration] = field(default_factory=list)
    pending: list[Migration] = field(default_factory=list)
    current: str | None = None
    mismatches: list[ChecksumMismatch] = field(default_factory=list)


class MigrationError(RuntimeError):
    """Migration framework failure."""


class MigrationLockError(MigrationError):
    """Couldn't acquire the migration lock (another runner is active)."""


# ---------------------------------------------------------------------------
# Lock helper
#
# Uses fcntl on POSIX and a no-op fallback elsewhere. The lock file lives
# next to the SQLite DB so the lock and the data are co-located on the
# same filesystem.


@contextlib.contextmanager
def _file_lock(lock_path: Path, *, blocking: bool = True):
    """Acquire an exclusive lock on ``lock_path``.

    Blocking mode (default) waits indefinitely; non-blocking raises
    :class:`MigrationLockError` immediately if the lock is held.
    """

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Touch the file so flock has something to lock against.
    fh = lock_path.open("a+")
    try:
        try:
            import fcntl

            flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            try:
                fcntl.flock(fh.fileno(), flags)
            except BlockingIOError as exc:
                raise MigrationLockError(
                    f"another migration runner holds {lock_path}"
                ) from exc
        except ImportError:  # pragma: no cover -- non-POSIX fallback
            pass
        yield fh
    finally:
        try:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):  # pragma: no cover
            pass
        fh.close()


# ---------------------------------------------------------------------------
# Discovery


def discover_migrations(migrations_dir: Path) -> list[Migration]:
    """Read all migration files from ``migrations_dir``.

    Files are sorted by ID (lexical sort on the filename prefix, so the
    convention of zero-padding 4-digit prefixes keeps order stable).
    Rollback files (``<id>_rollback.sql``) are skipped.
    """

    if not migrations_dir.is_dir():
        raise MigrationError(
            f"migrations directory does not exist: {migrations_dir}"
        )
    migrations: list[Migration] = []
    for path in sorted(migrations_dir.iterdir()):
        if not path.is_file():
            continue
        # Skip rollback siblings before the forward-file regex check.
        if _ROLLBACK_FILE_RE.match(path.name):
            continue
        m = _MIGRATION_FILE_RE.match(path.name)
        if not m:
            continue
        sql = path.read_text(encoding="utf-8")
        migrations.append(
            Migration(
                id=m.group("id"),
                path=path,
                sql=sql,
                checksum=sha256_of_text(sql),
            )
        )
    # Already sorted by filename, but tie-break by ID for safety.
    migrations.sort(key=lambda mig: mig.id)
    return migrations


def sha256_of_text(text: str) -> str:
    """Return the hex sha256 digest of ``text`` (UTF-8 encoded)."""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# SQL inspection helpers — used by the "already-applied" detector


def _tables_created_by(sql: str) -> list[str]:
    """Return the table names a migration SQL string creates.

    Used by the runner's heuristic for detecting whether a pending
    migration is a no-op against a legacy database that already has the
    same tables.
    """

    return [
        match.group(1).lower()
        for match in _CREATE_TABLE_RE.finditer(sql)
    ]


def _columns_added_by(sql: str) -> list[tuple[str, str]]:
    """Return ``(table, column)`` pairs added by ALTER TABLE statements."""

    return [
        (match.group(1).lower(), match.group(2).lower())
        for match in _ALTER_TABLE_ADD_COL_RE.finditer(sql)
    ]


# ---------------------------------------------------------------------------
# Runner


class MigrationRunner:
    """Apply forward migrations against an aiosqlite database.

    The runner is constructed with an aiosqlite-compatible db path and a
    migrations directory. A single instance is safe across calls, but
    concurrent ``apply_pending`` invocations across processes serialise via
    the file lock.
    """

    def __init__(
        self,
        db_path: Path,
        migrations_dir: Path,
        *,
        lock_path: Optional[Path] = None,  # noqa: UP007, UP045
    ) -> None:
        self._db_path = Path(db_path)
        self._migrations_dir = Path(migrations_dir)
        # Default lock co-located with the data file.
        self._lock_path = lock_path or (self._db_path.parent / ".migration.lock")

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def migrations_dir(self) -> Path:
        return self._migrations_dir

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    # ---------------------------------------------------------------- listing

    def list_migrations_on_disk(self) -> list[Migration]:
        """Return the on-disk migrations sorted by ID."""

        return discover_migrations(self._migrations_dir)

    async def list_applied(self) -> list[AppliedMigration]:
        """Return all rows from ``schema_migrations`` (ID-ascending).

        Returns an empty list if the table doesn't exist yet.
        """

        on_disk = {mig.id: mig for mig in self.list_migrations_on_disk()}
        async with aiosqlite.connect(self._db_path) as conn:
            conn.row_factory = aiosqlite.Row
            await self._ensure_tracking_table(conn)
            cur = await conn.execute(
                "SELECT id, applied_at, duration_ms, checksum "
                "FROM schema_migrations ORDER BY id ASC"
            )
            rows = await cur.fetchall()
            await cur.close()
        out: list[AppliedMigration] = []
        for row in rows:
            mig = on_disk.get(row["id"])
            sql = mig.sql if mig else ""
            path = mig.path if mig else self._migrations_dir / f"{row['id']}.sql"
            out.append(
                AppliedMigration(
                    id=row["id"],
                    path=path,
                    sql=sql,
                    checksum=row["checksum"],
                    applied_at=_parse_ts(row["applied_at"]),
                    duration_ms=int(row["duration_ms"]),
                )
            )
        return out

    async def list_pending(self) -> list[Migration]:
        """Return on-disk migrations not yet recorded in ``schema_migrations``."""

        applied = {a.id for a in await self.list_applied()}
        return [mig for mig in self.list_migrations_on_disk() if mig.id not in applied]

    async def status(self) -> MigrationStatus:
        """Return a :class:`MigrationStatus` snapshot."""

        applied = await self.list_applied()
        pending = await self.list_pending()
        mismatches = await self.verify_checksums()
        return MigrationStatus(
            applied=applied,
            pending=pending,
            current=applied[-1].id if applied else None,
            mismatches=mismatches,
        )

    # ---------------------------------------------------------------- apply

    async def apply_pending(
        self,
        *,
        blocking_lock: bool = True,
    ) -> list[AppliedMigration]:
        """Apply all pending migrations in order. Returns the new applications.

        Each migration is applied inside its own transaction, with the
        ``schema_migrations`` insert in the same transaction so apply +
        record stay atomic. The whole loop runs under a file lock so
        concurrent runners serialise.

        ``blocking_lock=False`` raises :class:`MigrationLockError` instead
        of waiting. The HTTP endpoint uses non-blocking + 409.
        """

        with _file_lock(self._lock_path, blocking=blocking_lock):
            return await self._apply_locked(target_id=None)

    async def apply_to(
        self,
        target_id: str,
        *,
        blocking_lock: bool = True,
    ) -> list[AppliedMigration]:
        """Apply forward migrations up to and including ``target_id``.

        Raises :class:`MigrationError` if ``target_id`` doesn't match any
        on-disk migration, or if it's already past the current applied
        position (rollback is a v0.6 feature).
        """

        # Validate target_id exists somewhere on disk.
        ids = [mig.id for mig in self.list_migrations_on_disk()]
        if target_id not in ids:
            # Permit short prefixes ("0003" → "0003_workflows") for ergonomics.
            short_matches = [i for i in ids if i.split("_", 1)[0] == target_id]
            if len(short_matches) == 1:
                target_id = short_matches[0]
            else:
                raise MigrationError(
                    f"unknown migration id: {target_id!r}; "
                    f"on-disk ids: {ids}"
                )
        applied = await self.list_applied()
        if applied and applied[-1].id > target_id:
            raise MigrationError(
                f"target {target_id!r} is older than current "
                f"applied {applied[-1].id!r}; rollback support arrives in v0.6"
            )
        with _file_lock(self._lock_path, blocking=blocking_lock):
            return await self._apply_locked(target_id=target_id)

    # ---------------------------------------------------------------- internals

    async def _apply_locked(
        self,
        *,
        target_id: str | None,
    ) -> list[AppliedMigration]:
        """Lock-held inner loop. Applies each pending migration in sequence."""

        results: list[AppliedMigration] = []
        async with aiosqlite.connect(self._db_path) as conn:
            await conn.execute("PRAGMA foreign_keys=ON")
            conn.row_factory = aiosqlite.Row
            await self._ensure_tracking_table(conn)
            applied_ids = await self._fetch_applied_ids(conn)

            for mig in self.list_migrations_on_disk():
                if mig.id in applied_ids:
                    continue
                if target_id is not None and mig.id > target_id:
                    break
                applied = await self._apply_single(conn, mig)
                results.append(applied)
        return results

    async def _apply_single(
        self,
        conn: aiosqlite.Connection,
        mig: Migration,
    ) -> AppliedMigration:
        """Apply ``mig`` inside its own transaction.

        Idempotency strategy:

        * Detect tables the migration creates. If every CREATE TABLE
          statement targets an already-existing table AND every ALTER
          TABLE ADD COLUMN targets an already-existing column, treat the
          migration as a no-op against a legacy database — record applied
          without running the SQL.
        * Otherwise: BEGIN, executescript, INSERT into schema_migrations,
          COMMIT. On failure, ROLLBACK and re-raise as
          :class:`MigrationError`.
        """

        is_noop = await self._is_already_applied(conn, mig)
        start = time.perf_counter()
        try:
            await conn.execute("BEGIN")
            if not is_noop:
                # executescript would auto-commit; we manage the txn ourselves.
                # Run statement-by-statement so a syntax error rolls back.
                for stmt in _split_statements(mig.sql):
                    if not stmt.strip():
                        continue
                    try:
                        await conn.execute(stmt)
                    except aiosqlite.OperationalError as exc:
                        msg = str(exc).lower()
                        # SQLite raises "duplicate column name" on a re-applied
                        # ALTER TABLE ... ADD COLUMN. Treat that as benign so
                        # the migration is still considered idempotent across
                        # legacy databases.
                        if "duplicate column" in msg:
                            continue
                        raise
            duration_ms = int((time.perf_counter() - start) * 1000)
            applied_at = datetime.now(UTC).replace(microsecond=0)
            await conn.execute(
                "INSERT INTO schema_migrations (id, applied_at, duration_ms, checksum) "
                "VALUES (?, ?, ?, ?)",
                (mig.id, _iso(applied_at), duration_ms, mig.checksum),
            )
            await conn.commit()
        except Exception as exc:
            try:
                await conn.rollback()
            except Exception:  # pragma: no cover -- defensive
                pass
            raise MigrationError(
                f"migration {mig.id!r} failed: {exc}"
            ) from exc
        return AppliedMigration(
            id=mig.id,
            path=mig.path,
            sql=mig.sql,
            checksum=mig.checksum,
            applied_at=applied_at,
            duration_ms=duration_ms,
        )

    async def _is_already_applied(
        self,
        conn: aiosqlite.Connection,
        mig: Migration,
    ) -> bool:
        """Heuristic: can ``mig`` be skipped because everything it makes already exists?

        Returns ``True`` only if all tables it would create exist AND all
        columns it would add already exist. Indices and other statements
        are ignored — they're idempotent (``IF NOT EXISTS``) anyway.

        Returns ``False`` for migrations that contain no CREATE TABLE /
        ALTER TABLE — those always run (their statements are themselves
        idempotent).
        """

        tables = _tables_created_by(mig.sql)
        added_cols = _columns_added_by(mig.sql)
        if not tables and not added_cols:
            return False

        existing_tables = await self._existing_tables(conn)
        for table in tables:
            if table not in existing_tables:
                return False
        for table, column in added_cols:
            cols = await self._table_columns(conn, table)
            if column not in cols:
                return False
        return True

    @staticmethod
    async def _existing_tables(conn: aiosqlite.Connection) -> set[str]:
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        rows = await cur.fetchall()
        await cur.close()
        return {row[0].lower() for row in rows}

    @staticmethod
    async def _table_columns(conn: aiosqlite.Connection, table: str) -> set[str]:
        # PRAGMA table_info doesn't accept parameters, but the table name
        # comes from our own SQL parsing — never user input.
        cur = await conn.execute(f"PRAGMA table_info({table})")
        rows = await cur.fetchall()
        await cur.close()
        return {row[1].lower() for row in rows}

    @staticmethod
    async def _fetch_applied_ids(conn: aiosqlite.Connection) -> set[str]:
        cur = await conn.execute("SELECT id FROM schema_migrations")
        rows = await cur.fetchall()
        await cur.close()
        return {row[0] for row in rows}

    @staticmethod
    async def _ensure_tracking_table(conn: aiosqlite.Connection) -> None:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "  id TEXT PRIMARY KEY,"
            "  applied_at TIMESTAMP NOT NULL,"
            "  duration_ms INTEGER NOT NULL,"
            "  checksum TEXT NOT NULL"
            ")"
        )
        await conn.commit()

    # -------------------------------------------------------------- checksums

    async def verify_checksums(self) -> list[ChecksumMismatch]:
        """Recompute checksums and report any drift from stored values."""

        on_disk = {mig.id: mig for mig in self.list_migrations_on_disk()}
        applied = await self.list_applied()
        out: list[ChecksumMismatch] = []
        for record in applied:
            mig = on_disk.get(record.id)
            if mig is None:
                # The file is gone but the row remains. We treat the
                # current checksum as empty so it's flagged.
                out.append(
                    ChecksumMismatch(
                        id=record.id,
                        stored_checksum=record.checksum,
                        current_checksum="",
                    )
                )
                continue
            if mig.checksum != record.checksum:
                out.append(
                    ChecksumMismatch(
                        id=record.id,
                        stored_checksum=record.checksum,
                        current_checksum=mig.checksum,
                    )
                )
        return out

    # --------------------------------------------------------------- scaffold

    def create_migration(self, label: str) -> Path:
        """Scaffold a new migration file in :attr:`migrations_dir`.

        The new file gets the next free numeric prefix and a slugified
        version of ``label``. Returns the new path.
        """

        slug = _slugify(label)
        if not slug:
            raise MigrationError("migration label produced empty slug")

        existing = self.list_migrations_on_disk()
        if existing:
            last = existing[-1].id.split("_", 1)[0]
            next_idx = int(last) + 1
        else:
            next_idx = 1
        new_id = f"{next_idx:04d}_{slug}"
        new_path = self._migrations_dir / f"{new_id}.sql"
        if new_path.exists():
            raise MigrationError(f"file already exists: {new_path}")
        header = (
            f"-- Migration: {new_id}\n"
            f"-- TODO: describe purpose; prefer CREATE/ALTER ... IF NOT EXISTS\n"
            f"-- Generated by `migrate --create {label!r}`.\n\n"
        )
        new_path.write_text(header, encoding="utf-8")
        return new_path


# ---------------------------------------------------------------------------
# Helpers


def _split_statements(sql: str) -> list[str]:
    """Split a SQL script into top-level statements at semicolons.

    Naive but adequate for our migration files (no multi-statement triggers,
    no procedural blocks). Comment lines (``--``) are preserved with their
    statement so SQLite parses them as no-ops.
    """

    stmts: list[str] = []
    buf: list[str] = []
    for line in sql.splitlines(keepends=True):
        buf.append(line)
        # Cheap end-of-statement detection. We don't need to handle string
        # literals containing ``;`` because none of our migrations do.
        if line.rstrip().endswith(";"):
            stmts.append("".join(buf))
            buf = []
    if buf:
        stmts.append("".join(buf))
    return stmts


def _slugify(label: str) -> str:
    """Lower-case, replace non-alnum with ``_``, collapse repeats, trim."""

    out: list[str] = []
    for ch in label.strip().lower():
        if ch.isalnum():
            out.append(ch)
        else:
            out.append("_")
    slug = "".join(out)
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug.strip("_")


def _iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.isoformat()


def _parse_ts(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Path resolution
#
# Services call ``default_migrations_dir(__file__)`` to get the path to the
# ``migrations/`` directory next to the package.


def default_migrations_dir(package_init_file: str) -> Path:
    """Resolve the ``migrations/`` directory next to a service package.

    ``package_init_file`` is typically the ``__file__`` of the service's
    ``__init__.py`` or ``__main__.py``. The migrations directory sits two
    levels up from ``src/<package>/``: ``services/<name>/migrations/``.
    """

    pkg_path = Path(package_init_file).resolve()
    # services/<name>/src/<package>/<file> → services/<name>
    service_dir = pkg_path.parent.parent.parent
    return service_dir / "migrations"


__all__ = [
    "AppliedMigration",
    "ChecksumMismatch",
    "Migration",
    "MigrationError",
    "MigrationLockError",
    "MigrationRunner",
    "MigrationStatus",
    "default_migrations_dir",
    "discover_migrations",
    "sha256_of_text",
]
