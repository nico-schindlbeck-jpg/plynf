# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""SQLite-backed metadata + revocation store for issued tokens.

The store never persists the JWT itself — only the metadata you'd want for
introspection, audit, and revocation. JWTs are stateless capability tokens;
once minted, anyone who holds the secret can verify them. Revocation is
implemented as a ``revoked`` flag the verify path consults on every check.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from .exceptions import TenantAlreadyExists, TenantNotFound, TokenNotFound
from .models import RevocationEntry, Tenant, TokenInfo

UTC = timezone.utc  # noqa: UP017


SCHEMA = """
CREATE TABLE IF NOT EXISTS issued_tokens (
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

CREATE INDEX IF NOT EXISTS idx_tokens_agent
  ON issued_tokens(agent_id, issued_at DESC);
CREATE INDEX IF NOT EXISTS idx_tokens_tenant
  ON issued_tokens(tenant_id, issued_at DESC);
CREATE INDEX IF NOT EXISTS idx_tokens_expires
  ON issued_tokens(expires_at);
CREATE INDEX IF NOT EXISTS idx_tokens_revoked_at
  ON issued_tokens(revoked, revoked_at);

CREATE TABLE IF NOT EXISTS tenants (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  metadata TEXT NOT NULL DEFAULT '{}',
  created_at TIMESTAMP NOT NULL
);

-- v1.0 — per-tenant resource quotas + usage rollup.
CREATE TABLE IF NOT EXISTS tenant_quotas (
  tenant_id TEXT PRIMARY KEY,
  max_workspaces INTEGER NOT NULL DEFAULT 100,
  max_storage_gb REAL NOT NULL DEFAULT 10.0,
  max_channels_per_workspace INTEGER NOT NULL DEFAULT 50,
  max_workflows_per_workspace INTEGER NOT NULL DEFAULT 100,
  max_active_tokens INTEGER NOT NULL DEFAULT 1000,
  max_oauth_connections INTEGER NOT NULL DEFAULT 50,
  max_cost_usd_day REAL NOT NULL DEFAULT 100.0,
  max_cost_usd_month REAL NOT NULL DEFAULT 2000.0,
  max_invocations_per_minute INTEGER NOT NULL DEFAULT 600,
  updated_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS tenant_usage (
  tenant_id TEXT PRIMARY KEY,
  workspaces INTEGER NOT NULL DEFAULT 0,
  storage_gb REAL NOT NULL DEFAULT 0.0,
  active_tokens INTEGER NOT NULL DEFAULT 0,
  oauth_connections INTEGER NOT NULL DEFAULT 0,
  cost_usd_day REAL NOT NULL DEFAULT 0.0,
  cost_usd_month REAL NOT NULL DEFAULT 0.0,
  last_invocation_at TIMESTAMP,
  updated_at TIMESTAMP NOT NULL
);

-- v1.0 — GDPR Article 20 (data portability) export jobs.
CREATE TABLE IF NOT EXISTS export_jobs (
  export_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  requested_at TIMESTAMP NOT NULL,
  completed_at TIMESTAMP,
  expires_at TIMESTAMP,
  size_bytes INTEGER,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_export_jobs_tenant
  ON export_jobs(tenant_id, requested_at DESC);

-- v1.0 — GDPR Article 17 (erasure) cascade jobs.
CREATE TABLE IF NOT EXISTS delete_jobs (
  job_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  requested_at TIMESTAMP NOT NULL,
  completed_at TIMESTAMP,
  deleted_counts TEXT NOT NULL DEFAULT '{}',
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_delete_jobs_tenant
  ON delete_jobs(tenant_id, requested_at DESC);

-- v1.0 — Two-phase delete confirm tokens. Short-lived (~10 min), one-shot.
CREATE TABLE IF NOT EXISTS delete_confirm_tokens (
  confirm_token TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  created_at TIMESTAMP NOT NULL,
  expires_at TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_delete_confirm_tokens_tenant
  ON delete_confirm_tokens(tenant_id);
"""

DEFAULT_TENANT_ID = "default"
DEFAULT_TENANT_NAME = "Default"


def _ensure_parent_dir(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)


async def init_db(db_path: Path) -> None:
    """Initialise the database (idempotent).

    Creates the parent directory if missing, applies the schema, turns on
    WAL + foreign keys, and seeds the ``default`` tenant if missing. Safe to
    call repeatedly.
    """

    _ensure_parent_dir(db_path)
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.executescript(SCHEMA)
        # Seed the default tenant. The INSERT OR IGNORE keeps init_db
        # idempotent across restarts and across older DBs that may already
        # have a row (e.g. if an operator created it manually).
        now = datetime.now(UTC).replace(microsecond=0).isoformat()
        await conn.execute(
            "INSERT OR IGNORE INTO tenants (id, name, metadata, created_at) "
            "VALUES (?, ?, '{}', ?)",
            (DEFAULT_TENANT_ID, DEFAULT_TENANT_NAME, now),
        )
        await conn.commit()


@contextlib.asynccontextmanager
async def connect(db_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    """Open a fresh aiosqlite connection with row-factory + sane PRAGMAs."""

    _ensure_parent_dir(db_path)
    conn = await aiosqlite.connect(db_path)
    try:
        await conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = aiosqlite.Row
        yield conn
    finally:
        await conn.close()


def _iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.isoformat()


def _parse_ts(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _row_to_info(row: aiosqlite.Row) -> TokenInfo:
    issued_at = _parse_ts(row["issued_at"])
    expires_at = _parse_ts(row["expires_at"])
    assert issued_at is not None and expires_at is not None  # noqa: S101
    return TokenInfo(
        jti=row["jti"],
        agent_id=row["agent_id"],
        tenant_id=row["tenant_id"],
        workspace_id=row["workspace_id"],
        scopes=json.loads(row["scopes"] or "[]"),
        issued_at=issued_at,
        expires_at=expires_at,
        revoked=bool(row["revoked"]),
        revoked_at=_parse_ts(row["revoked_at"]),
        metadata=json.loads(row["metadata"] or "{}"),
    )


class TokenStore:
    """CRUD against ``issued_tokens`` plus a revocation cache.

    The revocation cache is a simple in-memory ``set[str]`` of revoked JTIs
    populated lazily on the first verify. Every revoke writes to both SQLite
    and the cache; every is-revoked check reads the cache only. This keeps
    the verify path off the disk for the hot read.
    """

    def __init__(self, db_path: Path, *, coordination: Any | None = None) -> None:
        self._db_path = db_path
        self._revoked_cache: set[str] | None = None
        # v1.1 — coordination backend. When supplied (typically a
        # ``RedisBackend``) every revoke pushes the JTI into a shared
        # ``revoked_jtis`` set so peer replicas see it immediately
        # without waiting for the polling cycle. Reads still hit the
        # local cache first for speed; the coordination set is the
        # propagation channel, not the authority.
        self._coordination = coordination

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def coordination(self) -> Any | None:
        return self._coordination

    def attach_coordination(self, coordination: Any) -> None:
        """Late-bind the coordination backend (used by ``api.create_app``)."""

        self._coordination = coordination

    async def insert(
        self,
        *,
        jti: str,
        agent_id: str,
        tenant_id: str,
        workspace_id: str | None,
        scopes: list[str],
        issued_at: datetime,
        expires_at: datetime,
        metadata: dict[str, Any] | None = None,
    ) -> TokenInfo:
        """Persist token metadata. Returns the resulting :class:`TokenInfo`."""

        meta_json = json.dumps(metadata or {}, sort_keys=True)
        scopes_json = json.dumps(list(scopes))
        async with connect(self._db_path) as conn:
            await conn.execute(
                "INSERT INTO issued_tokens "
                "(jti, agent_id, tenant_id, workspace_id, scopes, "
                " issued_at, expires_at, revoked, revoked_at, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, ?)",
                (
                    jti,
                    agent_id,
                    tenant_id,
                    workspace_id,
                    scopes_json,
                    _iso(issued_at),
                    _iso(expires_at),
                    meta_json,
                ),
            )
            await conn.commit()
        return TokenInfo(
            jti=jti,
            agent_id=agent_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            scopes=list(scopes),
            issued_at=issued_at,
            expires_at=expires_at,
            revoked=False,
            revoked_at=None,
            metadata=metadata or {},
        )

    async def get(self, jti: str) -> TokenInfo:
        """Fetch a token by JTI. Raises :class:`TokenNotFound` if absent."""

        async with connect(self._db_path) as conn:
            cur = await conn.execute(
                "SELECT * FROM issued_tokens WHERE jti=?",
                (jti,),
            )
            row = await cur.fetchone()
            await cur.close()
        if row is None:
            raise TokenNotFound(jti)
        return _row_to_info(row)

    async def list_by_agent(
        self,
        agent_id: str,
        *,
        limit: int = 100,
    ) -> list[TokenInfo]:
        async with connect(self._db_path) as conn:
            cur = await conn.execute(
                "SELECT * FROM issued_tokens WHERE agent_id=? "
                "ORDER BY issued_at DESC LIMIT ?",
                (agent_id, limit),
            )
            rows = await cur.fetchall()
            await cur.close()
        return [_row_to_info(r) for r in rows]

    async def list_by_tenant(
        self,
        tenant_id: str,
        *,
        limit: int = 100,
    ) -> list[TokenInfo]:
        async with connect(self._db_path) as conn:
            cur = await conn.execute(
                "SELECT * FROM issued_tokens WHERE tenant_id=? "
                "ORDER BY issued_at DESC LIMIT ?",
                (tenant_id, limit),
            )
            rows = await cur.fetchall()
            await cur.close()
        return [_row_to_info(r) for r in rows]

    async def revoke(self, jti: str) -> TokenInfo:
        """Flip the revoked flag (idempotent). Returns the post-state info."""

        info = await self.get(jti)  # raises TokenNotFound if missing
        revoked_at = info.revoked_at or datetime.now(UTC).replace(microsecond=0)
        async with connect(self._db_path) as conn:
            await conn.execute(
                "UPDATE issued_tokens SET revoked=1, revoked_at=? WHERE jti=?",
                (_iso(revoked_at), jti),
            )
            await conn.commit()

        # Keep the in-memory cache hot if it's been populated.
        if self._revoked_cache is not None:
            self._revoked_cache.add(jti)

        # v1.1 — push to the coordination set so peer replicas + downstream
        # services (gateway, workspace) pick it up via Redis without
        # waiting for the next polling cycle. TTL = expiry-since-now so
        # entries don't accumulate forever.
        if self._coordination is not None:
            try:
                ttl = max(
                    1,
                    int((info.expires_at - datetime.now(UTC)).total_seconds()),
                )
                await self._coordination.add_to_set(
                    "revoked_jtis",
                    jti,
                    ttl_seconds=ttl,
                )
            except Exception:  # noqa: BLE001 — propagation is best-effort
                pass

        return TokenInfo(
            jti=info.jti,
            agent_id=info.agent_id,
            tenant_id=info.tenant_id,
            workspace_id=info.workspace_id,
            scopes=info.scopes,
            issued_at=info.issued_at,
            expires_at=info.expires_at,
            revoked=True,
            revoked_at=revoked_at,
            metadata=info.metadata,
        )

    async def is_revoked(self, jti: str) -> bool:
        """True if ``jti`` is revoked.

        First call lazily populates the local cache from SQLite. Subsequent
        calls hit the in-memory cache.

        v1.1 — when a coordination backend is attached (typically Redis),
        we *also* consult the cluster-shared set on a cache miss so a
        revocation issued on a peer replica is honoured here within one
        request rather than one polling cycle. Best-effort: a Redis
        outage falls through to the local cache only.
        """

        if self._revoked_cache is None:
            self._revoked_cache = await self._load_revoked()
        if jti in self._revoked_cache:
            return True
        if self._coordination is not None:
            try:
                if await self._coordination.is_member("revoked_jtis", jti):
                    self._revoked_cache.add(jti)
                    return True
            except Exception:  # noqa: BLE001
                pass
        return False

    async def revoked_jtis(self) -> set[str]:
        """Return the in-memory set of revoked JTIs (populating if needed)."""

        if self._revoked_cache is None:
            self._revoked_cache = await self._load_revoked()
        return set(self._revoked_cache)

    async def reload_cache(self) -> int:
        """Re-read revoked JTIs from SQLite. Returns the new cache size."""

        self._revoked_cache = await self._load_revoked()
        return len(self._revoked_cache)

    async def _load_revoked(self) -> set[str]:
        async with connect(self._db_path) as conn:
            cur = await conn.execute(
                "SELECT jti FROM issued_tokens WHERE revoked=1"
            )
            rows = await cur.fetchall()
            await cur.close()
        return {r["jti"] for r in rows}

    async def list_tokens(
        self,
        *,
        revoked: bool | None = None,
        since: datetime | None = None,
        agent_id: str | None = None,
        tenant_id: str | None = None,
        limit: int = 1000,
    ) -> list[TokenInfo]:
        """Filter-able token listing for revocation polling.

        ``since`` is interpreted against ``revoked_at`` when ``revoked=True``
        (so callers can ask "any tokens revoked since the last poll"); against
        ``issued_at`` otherwise. ``limit`` is capped at 1000 to keep responses
        bounded.
        """

        clauses: list[str] = []
        params: list[Any] = []

        if revoked is True:
            clauses.append("revoked = 1")
            if since is not None:
                clauses.append("revoked_at >= ?")
                params.append(_iso(since))
        elif revoked is False:
            clauses.append("revoked = 0")
            if since is not None:
                clauses.append("issued_at >= ?")
                params.append(_iso(since))
        elif since is not None:
            clauses.append("issued_at >= ?")
            params.append(_iso(since))

        if agent_id is not None:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order_by = "ORDER BY revoked_at DESC" if revoked is True else "ORDER BY issued_at DESC"
        capped = max(1, min(int(limit), 1000))
        sql = f"SELECT * FROM issued_tokens {where} {order_by} LIMIT ?"
        params.append(capped)

        async with connect(self._db_path) as conn:
            cur = await conn.execute(sql, tuple(params))
            rows = await cur.fetchall()
            await cur.close()
        return [_row_to_info(r) for r in rows]

    # ------------------------------------------------------------- v0.6 fed.
    # Federated revocation: peer replicas poll ``GET /v1/revocations`` to
    # learn about tokens revoked on other nodes. The query is keyed by
    # ``revoked_at`` so the caller can use it as a forward cursor.

    async def list_revocations(
        self,
        since_unix: int,
        limit: int = 1000,
    ) -> tuple[list[RevocationEntry], bool]:
        """Return revoked-token entries newer than ``since_unix`` (exclusive).

        Returns ``(entries, has_more)``. ``has_more`` is True iff there were
        more than ``limit`` matching rows; callers paginate by re-polling
        with ``since=last_revoked_at``.

        ``limit`` is capped at 2000 to keep responses bounded; values <1
        coerce to 1.
        """

        capped = max(1, min(int(limit), 2000))
        cutoff = datetime.fromtimestamp(int(since_unix), tz=UTC)
        # Fetch one extra row beyond ``capped`` so we can compute has_more
        # without a second COUNT(*).
        async with connect(self._db_path) as conn:
            cur = await conn.execute(
                "SELECT jti, agent_id, tenant_id, revoked_at "
                "FROM issued_tokens "
                "WHERE revoked = 1 AND revoked_at > ? "
                "ORDER BY revoked_at ASC LIMIT ?",
                (_iso(cutoff), capped + 1),
            )
            rows = await cur.fetchall()
            await cur.close()
        has_more = len(rows) > capped
        rows = rows[:capped]
        out: list[RevocationEntry] = []
        for row in rows:
            revoked_at = _parse_ts(row["revoked_at"])
            assert revoked_at is not None  # noqa: S101
            out.append(
                RevocationEntry(
                    jti=row["jti"],
                    revoked_at=revoked_at,
                    agent_id=row["agent_id"],
                    tenant_id=row["tenant_id"],
                )
            )
        return out, has_more

    async def revocation_stats(self) -> tuple[int, int, int]:
        """Return ``(total, since_24h, since_1h)`` revocation counts."""

        now = datetime.now(UTC)
        h1 = now - timedelta(hours=1)
        h24 = now - timedelta(hours=24)
        async with connect(self._db_path) as conn:
            cur = await conn.execute(
                "SELECT COUNT(*) AS c FROM issued_tokens WHERE revoked = 1"
            )
            row = await cur.fetchone()
            total = int(row["c"]) if row is not None else 0
            await cur.close()

            cur = await conn.execute(
                "SELECT COUNT(*) AS c FROM issued_tokens "
                "WHERE revoked = 1 AND revoked_at > ?",
                (_iso(h24),),
            )
            row = await cur.fetchone()
            since_24h = int(row["c"]) if row is not None else 0
            await cur.close()

            cur = await conn.execute(
                "SELECT COUNT(*) AS c FROM issued_tokens "
                "WHERE revoked = 1 AND revoked_at > ?",
                (_iso(h1),),
            )
            row = await cur.fetchone()
            since_1h = int(row["c"]) if row is not None else 0
            await cur.close()
        return total, since_24h, since_1h


class TenantStore:
    """CRUD against the ``tenants`` table.

    Lives next to :class:`TokenStore` because the identity service is the
    source of truth for tenancy. Workspace + Gateway derive their view from
    the ``tenant_id`` claim on every JWT.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    @property
    def db_path(self) -> Path:
        return self._db_path

    async def create(
        self,
        *,
        tenant_id: str,
        name: str,
        metadata: dict[str, Any] | None = None,
    ) -> Tenant:
        """Insert a tenant. Raises :class:`TenantAlreadyExists` on conflict."""

        now = datetime.now(UTC).replace(microsecond=0)
        meta_json = json.dumps(metadata or {}, sort_keys=True)
        async with connect(self._db_path) as conn:
            try:
                await conn.execute(
                    "INSERT INTO tenants (id, name, metadata, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (tenant_id, name, meta_json, _iso(now)),
                )
                await conn.commit()
            except aiosqlite.IntegrityError as exc:
                raise TenantAlreadyExists(tenant_id) from exc
        return Tenant(
            id=tenant_id,
            name=name,
            metadata=metadata or {},
            created_at=now,
        )

    async def get(self, tenant_id: str) -> Tenant:
        async with connect(self._db_path) as conn:
            cur = await conn.execute(
                "SELECT * FROM tenants WHERE id = ?",
                (tenant_id,),
            )
            row = await cur.fetchone()
            await cur.close()
        if row is None:
            raise TenantNotFound(tenant_id)
        created_at = _parse_ts(row["created_at"])
        assert created_at is not None  # noqa: S101
        return Tenant(
            id=row["id"],
            name=row["name"],
            metadata=json.loads(row["metadata"] or "{}"),
            created_at=created_at,
        )

    async def list(self) -> list[Tenant]:
        async with connect(self._db_path) as conn:
            cur = await conn.execute(
                "SELECT * FROM tenants ORDER BY created_at ASC, id ASC"
            )
            rows = await cur.fetchall()
            await cur.close()
        out: list[Tenant] = []
        for row in rows:
            created_at = _parse_ts(row["created_at"])
            assert created_at is not None  # noqa: S101
            out.append(
                Tenant(
                    id=row["id"],
                    name=row["name"],
                    metadata=json.loads(row["metadata"] or "{}"),
                    created_at=created_at,
                )
            )
        return out


__all__ = ["DEFAULT_TENANT_ID", "DEFAULT_TENANT_NAME", "SCHEMA", "TenantStore", "TokenStore", "init_db"]
