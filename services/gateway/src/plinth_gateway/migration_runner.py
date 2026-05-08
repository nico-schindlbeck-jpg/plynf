# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Schema migration runner for the workspace service.

The runner reads forward migration files from ``migrations/``, applies any
that haven't been recorded in ``schema_migrations``, and tracks each
application's checksum and duration. v0.6 adds rollback execution: a
sibling ``<id>_rollback.sql`` file is run in reverse order to undo applied
migrations.

Design constraints
------------------

1. **Atomicity per file** — Each migration runs in its own transaction.
   A SQL error rolls back the file fully; no half-state. Same for
   rollback execution.
2. **Locking** — A file-system lock at ``$DATA_DIR/.migration.lock`` keeps
   concurrent processes serialised. Acquired with ``fcntl.flock``; released
   on context exit. The Postgres equivalent (``pg_advisory_lock``) is not
   wired in v0.5 because the workspace service uses SQLite locally.
   Rollback uses the same lock so apply + rollback can't interleave.
3. **Backwards compatibility** — On first run against an existing
   v0.1–v0.4 database, the runner inspects each pending migration: if
   every CREATE TABLE statement targets a table that already exists, the
   migration is recorded as applied without executing. This lets databases
   provisioned by the legacy CREATE-IF-NOT-EXISTS bootstrap upgrade
   cleanly.
4. **Checksums** — Each applied migration records ``sha256(file.read_bytes())``.
   :meth:`verify_checksums` compares the stored value against a fresh hash;
   a mismatch means someone edited history. v0.6 also stores a
   ``rollback_checksum`` (nullable) when a sibling rollback file exists.

Migration file format
---------------------

A migration is a plain ``.sql`` file. The filename's prefix (everything up
to the first ``.sql``) is the migration ID — e.g. ``0003_workflows.sql``
becomes ID ``0003_workflows``. IDs sort lexically in application order, so
zero-pad the numeric prefix.

Sibling rollback files (``<id>_rollback.sql``) are executed in reverse
order by :meth:`MigrationRunner.rollback_to`. Pre-rollback validation
checks every file is present *before* executing any of them — a missing
rollback halts before any state changes.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import time
from collections.abc import AsyncIterator
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
    rollback_path: Path | None = None
    rollback_sql: str | None = None
    rollback_checksum: str | None = None

    @property
    def has_rollback(self) -> bool:
        return self.rollback_path is not None


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
    rollback_checksum: str | None = None
    rollback_available: bool = False


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


@dataclass(frozen=True)
class RollbackPlanItem:
    """One step in a rollback plan: which migration to undo and how."""

    id: str
    rollback_path: Path
    rollback_sql: str
    rollback_checksum: str


@dataclass(frozen=True)
class RolledBackMigration:
    """A single migration that was rolled back, with timing info."""

    id: str
    rolled_back_at: datetime
    duration_ms: int


@dataclass(frozen=True)
class RollbackResult:
    """Outcome of executing a rollback plan.

    ``rolled_back`` records the migrations actually rolled back (in
    reverse-application order — newest first), each with the timestamp it
    completed and how many ms its rollback SQL took. ``failed`` carries
    the ID of the migration whose rollback errored (the loop stops on
    first failure); ``error_message`` explains what went wrong.
    """

    target: str
    rolled_back: list[RolledBackMigration] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: str | None = None
    error_message: str | None = None
    dry_run: bool = False


# Backwards-compat alias for v0.6-pre code that imported ``RollbackOutcome``.
# The dataclass is renamed to match the spec; the alias keeps imports working.
RollbackOutcome = RollbackResult


class MigrationError(RuntimeError):
    """Migration framework failure."""


class MigrationNotFound(MigrationError):
    """The requested migration ID isn't on disk."""


class MigrationRollbackMissing(MigrationError):
    """A rollback file is required but absent for at least one migration."""

    def __init__(self, missing_ids: list[str]) -> None:
        self.missing_ids = list(missing_ids)
        super().__init__(
            "rollback files missing for: " + ", ".join(missing_ids)
        )


class MigrationRollbackFailed(MigrationError):
    """Executing a rollback file raised an error.

    Carries the failing migration ID so callers (e.g. the HTTP endpoint)
    can include it in the structured error envelope.
    """

    def __init__(self, migration_id: str, original: Exception) -> None:
        self.migration_id = migration_id
        self.original = original
        super().__init__(
            f"rollback for {migration_id!r} failed: {original}"
        )


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
    Rollback siblings (``<id>_rollback.sql``) are attached to the matching
    forward migration via the :class:`Migration.rollback_path` /
    :attr:`Migration.rollback_sql` / :attr:`Migration.rollback_checksum`
    fields. A migration without a rollback file simply has those set to
    ``None``.
    """

    if not migrations_dir.is_dir():
        raise MigrationError(
            f"migrations directory does not exist: {migrations_dir}"
        )

    # Two-pass scan: collect rollbacks first, then attach them to the
    # forward migrations. Keeps the regex match tight per file.
    rollbacks: dict[str, Path] = {}
    forward_paths: list[tuple[str, Path]] = []
    for path in sorted(migrations_dir.iterdir()):
        if not path.is_file():
            continue
        rb_match = _ROLLBACK_FILE_RE.match(path.name)
        if rb_match:
            rollbacks[rb_match.group("id")] = path
            continue
        m = _MIGRATION_FILE_RE.match(path.name)
        if not m:
            continue
        forward_paths.append((m.group("id"), path))

    migrations: list[Migration] = []
    for mig_id, path in forward_paths:
        sql = path.read_text(encoding="utf-8")
        rb_path = rollbacks.get(mig_id)
        rb_sql: str | None = None
        rb_checksum: str | None = None
        if rb_path is not None:
            rb_sql = rb_path.read_text(encoding="utf-8")
            rb_checksum = sha256_of_text(rb_sql)
        migrations.append(
            Migration(
                id=mig_id,
                path=path,
                sql=sql,
                checksum=sha256_of_text(sql),
                rollback_path=rb_path,
                rollback_sql=rb_sql,
                rollback_checksum=rb_checksum,
            )
        )
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
        database_url: str = "",
        service_name: str = "gateway",
    ) -> None:
        self._db_path = Path(db_path)
        self._migrations_dir = Path(migrations_dir)
        # Default lock co-located with the data file.
        self._lock_path = lock_path or (self._db_path.parent / ".migration.lock")
        # v0.6 — Postgres advisory locks. When ``database_url`` points to
        # a Postgres DSN, ``_acquire_lock`` uses ``pg_advisory_lock`` so
        # multiple replicas serialise via the database itself instead of a
        # local filesystem lock. Empty string keeps the SQLite path
        # untouched.
        self._database_url = database_url or ""
        self._service_name = service_name

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def migrations_dir(self) -> Path:
        return self._migrations_dir

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    @property
    def database_url(self) -> str:
        return self._database_url

    @property
    def service_name(self) -> str:
        return self._service_name

    # --------------------------------------------------------- v0.6 locking
    #
    # Picks between fcntl (SQLite / local file) and pg_advisory_lock
    # (Postgres replicas). The advisory lock identifier is a stable hash
    # of ``plinth_migrations_<service_name>`` so all replicas of one
    # service contend for the same lock, but services don't block each
    # other.

    def _is_postgres(self) -> bool:
        url = self._database_url.lower()
        return url.startswith("postgres://") or url.startswith("postgresql://") or url.startswith(
            "postgresql+asyncpg://"
        )

    def _compute_lock_id(self) -> int:
        """Stable signed-int32 from the service name.

        Postgres advisory locks accept a single ``bigint`` or two
        ``int4`` values. We use the signed-int32 form (mask the high bit
        so the value stays positive) so the SQL works on the widest set
        of drivers.
        """

        digest = hashlib.sha256(
            f"plinth_migrations_{self._service_name}".encode("utf-8")
        ).digest()
        return int.from_bytes(digest[:4], byteorder="big") & 0x7FFFFFFF

    def _normalize_pg_url(self) -> str:
        """Strip the ``postgresql+asyncpg://`` prefix to a vanilla DSN."""

        url = self._database_url
        if url.startswith("postgresql+asyncpg://"):
            return "postgresql://" + url[len("postgresql+asyncpg://") :]
        return url

    @contextlib.asynccontextmanager
    async def _pg_advisory_lock(
        self,
        *,
        blocking: bool = True,
    ) -> AsyncIterator[None]:
        """Acquire ``pg_advisory_lock`` for the duration of the block.

        Defensive cleanup: even if the surrounding code raises, we attempt
        to issue ``pg_advisory_unlock``. If we can't talk to Postgres on
        the way out the connection close releases the session-scoped lock
        anyway — that's the whole point of advisory locks vs. relations.
        """

        try:
            import asyncpg  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover -- optional dep
            raise MigrationError(
                "asyncpg is required for Postgres advisory locks; "
                "install it with `pip install 'asyncpg>=0.29'`."
            ) from exc

        lock_id = self._compute_lock_id()
        dsn = self._normalize_pg_url()
        try:
            conn = await asyncpg.connect(dsn=dsn)
        except Exception as exc:  # noqa: BLE001
            raise MigrationLockError(
                f"unable to connect to Postgres for advisory lock: {exc}"
            ) from exc

        acquired = False
        try:
            if blocking:
                await conn.execute("SELECT pg_advisory_lock($1)", lock_id)
                acquired = True
            else:
                # ``pg_try_advisory_lock`` returns true iff acquired
                # immediately; mirrors the fcntl LOCK_NB semantics.
                row = await conn.fetchrow(
                    "SELECT pg_try_advisory_lock($1) AS got", lock_id
                )
                got = bool(row["got"]) if row is not None else False
                if not got:
                    raise MigrationLockError(
                        "another migration runner holds the Postgres "
                        f"advisory lock for service {self._service_name!r}"
                    )
                acquired = True
            yield
        finally:
            if acquired:
                try:
                    await conn.execute(
                        "SELECT pg_advisory_unlock($1)", lock_id
                    )
                except Exception:  # pragma: no cover -- defensive
                    # Connection close below releases session-scoped
                    # advisory locks anyway.
                    pass
            try:
                await conn.close()
            except Exception:  # pragma: no cover -- defensive
                pass

    @contextlib.asynccontextmanager
    async def _acquire_lock(
        self,
        *,
        blocking: bool = True,
    ) -> AsyncIterator[None]:
        """Pick the right lock primitive for the configured backend."""

        if self._is_postgres():
            async with self._pg_advisory_lock(blocking=blocking):
                yield
        else:
            with _file_lock(self._lock_path, blocking=blocking):
                yield

    # ---------------------------------------------------------------- listing

    def list_migrations_on_disk(self) -> list[Migration]:
        """Return the on-disk migrations sorted by ID."""

        return discover_migrations(self._migrations_dir)

    async def list_applied(self) -> list[AppliedMigration]:
        """Return all rows from ``schema_migrations`` (ID-ascending).

        Returns an empty list if the table doesn't exist yet. The
        ``rollback_available`` flag reflects whether the on-disk migration
        currently ships with a rollback sibling — independent of whether
        a ``rollback_checksum`` was stored at apply time (legacy rows
        applied before v0.6 have NULL ``rollback_checksum`` even when the
        rollback file exists today).
        """

        on_disk = {mig.id: mig for mig in self.list_migrations_on_disk()}
        async with aiosqlite.connect(self._db_path) as conn:
            conn.row_factory = aiosqlite.Row
            await self._ensure_tracking_table(conn)
            cur = await conn.execute(
                "SELECT id, applied_at, duration_ms, checksum, rollback_checksum "
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
                    rollback_checksum=row["rollback_checksum"],
                    rollback_available=mig is not None and mig.has_rollback,
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
        record stay atomic. The whole loop runs under a cluster-safe
        lock (``fcntl.flock`` for SQLite, ``pg_advisory_lock`` for
        Postgres) so concurrent runners serialise.

        ``blocking_lock=False`` raises :class:`MigrationLockError` instead
        of waiting. The HTTP endpoint uses non-blocking + 409.
        """

        async with self._acquire_lock(blocking=blocking_lock):
            return await self._apply_locked(target_id=None)

    async def apply_to(
        self,
        target_id: str,
        *,
        blocking_lock: bool = True,
    ) -> list[AppliedMigration]:
        """Apply forward migrations up to and including ``target_id``.

        Raises :class:`MigrationNotFound` if ``target_id`` doesn't match
        any on-disk migration. If ``target_id`` is older than the current
        applied head, callers must use :meth:`rollback_to` instead — this
        method only moves the schema forward.
        """

        target_id = self._resolve_target_id(target_id)
        applied = await self.list_applied()
        if applied and applied[-1].id > target_id:
            raise MigrationError(
                f"target {target_id!r} is older than current "
                f"applied {applied[-1].id!r}; use rollback_to() for backwards moves"
            )
        async with self._acquire_lock(blocking=blocking_lock):
            return await self._apply_locked(target_id=target_id)

    # ---------------------------------------------------------------- rollback

    async def rollback_to(
        self,
        target_id: str,
        *,
        dry_run: bool = False,
        blocking_lock: bool = True,
    ) -> RollbackResult:
        """Roll back applied migrations down to (and including) ``target_id``.

        Concretely: every applied migration with ``id > target_id`` is
        rolled back in reverse application order by executing its
        ``<id>_rollback.sql`` sibling and deleting the matching row from
        ``schema_migrations``. Each rollback runs in its own transaction.

        Pre-flight validation:

        * ``target_id`` must exist on disk (else :class:`MigrationNotFound`).
        * ``target_id`` must currently be applied (else :class:`MigrationError`).
        * Every migration in the rollback plan must ship with a rollback
          file (else :class:`MigrationRollbackMissing` — raised *before*
          any rollback runs, so partial state never appears for
          missing-file failures).
        * Each rollback file's checksum must match the value stored at apply
          time (if any was stored). Mismatch raises :class:`MigrationError`
          so tampered rollback files can't silently mutate the DB.

        Failure mid-rollback (a SQL error in one of the files) returns a
        :class:`RollbackResult` with ``failed`` set to the offending ID
        and ``error_message`` populated; earlier rollbacks remain
        committed.

        ``dry_run=True`` returns the plan without executing anything (and
        without acquiring the apply/rollback lock).
        """

        target_id = self._resolve_target_id(target_id)
        applied = await self.list_applied()
        applied_ids = {a.id for a in applied}
        if target_id not in applied_ids:
            raise MigrationError(
                f"target {target_id!r} is not currently applied; "
                f"nothing to roll back to"
            )
        # Plan: applied migrations with id > target, in REVERSE application
        # order (newest first).
        to_rollback = [a for a in applied if a.id > target_id]
        to_rollback.sort(key=lambda a: a.id, reverse=True)

        if not to_rollback:
            return RollbackResult(
                target=target_id,
                rolled_back=[],
                skipped=[],
                dry_run=dry_run,
            )

        # Every step needs a rollback file on disk. Build the plan by
        # mapping back to on-disk records — applied rows whose forward
        # file vanished from disk are already trouble; we surface them
        # the same way as a missing rollback so the operator restores
        # the file before retrying.
        on_disk = {mig.id: mig for mig in self.list_migrations_on_disk()}
        plan: list[RollbackPlanItem] = []
        missing: list[str] = []
        applied_by_id = {a.id: a for a in applied}
        for record in to_rollback:
            mig = on_disk.get(record.id)
            if mig is None or not mig.has_rollback:
                missing.append(record.id)
                continue
            assert mig.rollback_path is not None
            assert mig.rollback_sql is not None
            assert mig.rollback_checksum is not None
            # Checksum verification: if the apply recorded a rollback
            # checksum, it must match the current file content. Skip the
            # check for legacy rows applied pre-v0.6 (they have NULL).
            stored_checksum = applied_by_id[record.id].rollback_checksum
            if (
                stored_checksum is not None
                and stored_checksum != mig.rollback_checksum
            ):
                raise MigrationError(
                    f"rollback checksum mismatch for {record.id!r}: "
                    f"stored={stored_checksum} current={mig.rollback_checksum}"
                )
            plan.append(
                RollbackPlanItem(
                    id=record.id,
                    rollback_path=mig.rollback_path,
                    rollback_sql=mig.rollback_sql,
                    rollback_checksum=mig.rollback_checksum,
                )
            )

        if missing:
            raise MigrationRollbackMissing(missing)

        if dry_run:
            now = datetime.now(UTC).replace(microsecond=0)
            return RollbackResult(
                target=target_id,
                rolled_back=[
                    RolledBackMigration(
                        id=item.id,
                        rolled_back_at=now,
                        duration_ms=0,
                    )
                    for item in plan
                ],
                skipped=[],
                dry_run=True,
            )

        async with self._acquire_lock(blocking=blocking_lock):
            return await self._rollback_locked(target_id, plan)

    async def list_rollback_targets(self) -> list[str]:
        """IDs of currently-applied migrations that are valid rollback targets.

        A "valid target" is one whose rollback would still leave at least
        one migration applied — i.e. every applied migration *except* the
        very first. The current head is included (rolling back to the head
        is a legal no-op).
        """

        applied = await self.list_applied()
        if len(applied) <= 1:
            return []
        # Skip the very first applied migration — rolling back through it
        # would leave the schema empty.
        return [a.id for a in applied[1:]]

    def _resolve_target_id(self, target_id: str) -> str:
        """Resolve a target ID, accepting short prefixes for ergonomics."""

        ids = [mig.id for mig in self.list_migrations_on_disk()]
        if target_id in ids:
            return target_id
        short_matches = [i for i in ids if i.split("_", 1)[0] == target_id]
        if len(short_matches) == 1:
            return short_matches[0]
        raise MigrationNotFound(
            f"unknown migration id: {target_id!r}; "
            f"on-disk ids: {ids}"
        )

    async def _rollback_locked(
        self,
        target_id: str,
        plan: list[RollbackPlanItem],
    ) -> RollbackResult:
        """Lock-held rollback loop. Each item runs in its own transaction."""

        rolled_back: list[RolledBackMigration] = []
        async with aiosqlite.connect(self._db_path) as conn:
            await conn.execute("PRAGMA foreign_keys=ON")
            conn.row_factory = aiosqlite.Row
            await self._ensure_tracking_table(conn)

            for item in plan:
                start = time.perf_counter()
                try:
                    await conn.execute("BEGIN")
                    for stmt in _split_statements(item.rollback_sql):
                        if not stmt.strip():
                            continue
                        await conn.execute(stmt)
                    await conn.execute(
                        "DELETE FROM schema_migrations WHERE id = ?",
                        (item.id,),
                    )
                    await conn.commit()
                except Exception as exc:
                    try:
                        await conn.rollback()
                    except Exception:  # pragma: no cover -- defensive
                        pass
                    return RollbackResult(
                        target=target_id,
                        rolled_back=rolled_back,
                        skipped=[],
                        failed=item.id,
                        error_message=str(exc),
                        dry_run=False,
                    )
                duration_ms = int((time.perf_counter() - start) * 1000)
                rolled_back.append(
                    RolledBackMigration(
                        id=item.id,
                        rolled_back_at=datetime.now(UTC).replace(microsecond=0),
                        duration_ms=duration_ms,
                    )
                )
        return RollbackResult(
            target=target_id,
            rolled_back=rolled_back,
            skipped=[],
            dry_run=False,
        )

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
                "INSERT INTO schema_migrations "
                "(id, applied_at, duration_ms, checksum, rollback_checksum) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    mig.id,
                    _iso(applied_at),
                    duration_ms,
                    mig.checksum,
                    mig.rollback_checksum,
                ),
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
            rollback_checksum=mig.rollback_checksum,
            rollback_available=mig.has_rollback,
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
            "  checksum TEXT NOT NULL,"
            "  rollback_checksum TEXT"
            ")"
        )
        # Idempotent ALTER for databases provisioned before v0.6: if the
        # column doesn't exist, add it; if it does, swallow the duplicate.
        # We probe via PRAGMA table_info instead of a try/except so the
        # statement itself stays inside the conn's transactional context
        # without leaving a partial change on collision.
        cur = await conn.execute("PRAGMA table_info(schema_migrations)")
        cols = {row[1].lower() for row in await cur.fetchall()}
        await cur.close()
        if "rollback_checksum" not in cols:
            await conn.execute(
                "ALTER TABLE schema_migrations ADD COLUMN rollback_checksum TEXT"
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
    "MigrationNotFound",
    "MigrationRollbackFailed",
    "MigrationRollbackMissing",
    "MigrationRunner",
    "MigrationStatus",
    "RollbackOutcome",
    "RollbackPlanItem",
    "RollbackResult",
    "RolledBackMigration",
    "default_migrations_dir",
    "discover_migrations",
    "sha256_of_text",
]
