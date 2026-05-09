# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""GDPR export + delete orchestration.

Identity is the source of tenant truth, so it owns the orchestration of
the export (Article 20 portability) and delete (Article 17 erasure)
flows. The orchestrator:

* Calls workspace + gateway admin endpoints to fetch / delete their
  tenant-scoped data.
* Emits identity-owned data (tokens, oauth-connection metadata,
  tenant_quotas, tenants) directly from the identity DB.
* For exports: writes a ZIP under ``$DATA_DIR/exports/<export_id>.zip``
  and stamps a 24h ``expires_at``.
* For deletes: hard-removes identity-owned tenant rows last.
"""

from __future__ import annotations

import io
import json
import secrets
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from ulid import ULID

from .models import DeleteJob, ExportStatus
from .store import connect

UTC = timezone.utc

EXPORT_TTL_HOURS = 24
DELETE_CONFIRM_TTL_SECONDS = 600  # 10 minutes


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


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


def new_export_id() -> str:
    return f"exp_{ULID()}"


def new_delete_job_id() -> str:
    return f"del_{ULID()}"


def _row_to_export(row: Any) -> ExportStatus:
    return ExportStatus(
        export_id=row["export_id"],
        tenant_id=row["tenant_id"],
        status=row["status"],
        requested_at=_parse_ts(row["requested_at"]),  # type: ignore[arg-type]
        completed_at=_parse_ts(row["completed_at"]),
        expires_at=_parse_ts(row["expires_at"]),
        size_bytes=row["size_bytes"],
        error=row["error"],
    )


def _row_to_delete(row: Any) -> DeleteJob:
    return DeleteJob(
        job_id=row["job_id"],
        tenant_id=row["tenant_id"],
        status=row["status"],
        requested_at=_parse_ts(row["requested_at"]),  # type: ignore[arg-type]
        completed_at=_parse_ts(row["completed_at"]),
        deleted_counts=json.loads(row["deleted_counts"] or "{}"),
        error=row["error"],
    )


class ComplianceStore:
    """SQLite-backed CRUD for export + delete jobs and confirm tokens."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    @property
    def db_path(self) -> Path:
        return self._db_path

    # ---------------------------------------------------- exports

    async def create_export(self, tenant_id: str) -> ExportStatus:
        export_id = new_export_id()
        requested_at = _now()
        async with connect(self._db_path) as conn:
            await conn.execute(
                "INSERT INTO export_jobs "
                "(export_id, tenant_id, status, requested_at) "
                "VALUES (?, ?, 'pending', ?)",
                (export_id, tenant_id, _iso(requested_at)),
            )
            await conn.commit()
        return ExportStatus(
            export_id=export_id,
            tenant_id=tenant_id,
            status="pending",
            requested_at=requested_at,
        )

    async def get_export(self, export_id: str) -> ExportStatus | None:
        async with connect(self._db_path) as conn:
            cur = await conn.execute(
                "SELECT * FROM export_jobs WHERE export_id = ?",
                (export_id,),
            )
            row = await cur.fetchone()
            await cur.close()
        if row is None:
            return None
        return _row_to_export(row)

    async def update_export(
        self,
        export_id: str,
        *,
        status: str,
        completed_at: datetime | None = None,
        expires_at: datetime | None = None,
        size_bytes: int | None = None,
        error: str | None = None,
    ) -> None:
        async with connect(self._db_path) as conn:
            await conn.execute(
                "UPDATE export_jobs SET status=?, completed_at=?, "
                "expires_at=?, size_bytes=?, error=? WHERE export_id=?",
                (
                    status,
                    _iso(completed_at) if completed_at else None,
                    _iso(expires_at) if expires_at else None,
                    size_bytes,
                    error,
                    export_id,
                ),
            )
            await conn.commit()

    # ---------------------------------------------------- deletes

    async def create_delete_job(self, tenant_id: str) -> DeleteJob:
        job_id = new_delete_job_id()
        requested_at = _now()
        async with connect(self._db_path) as conn:
            await conn.execute(
                "INSERT INTO delete_jobs "
                "(job_id, tenant_id, status, requested_at, deleted_counts) "
                "VALUES (?, ?, 'pending', ?, '{}')",
                (job_id, tenant_id, _iso(requested_at)),
            )
            await conn.commit()
        return DeleteJob(
            job_id=job_id,
            tenant_id=tenant_id,
            status="pending",
            requested_at=requested_at,
            deleted_counts={},
        )

    async def get_delete_job(self, job_id: str) -> DeleteJob | None:
        async with connect(self._db_path) as conn:
            cur = await conn.execute(
                "SELECT * FROM delete_jobs WHERE job_id = ?",
                (job_id,),
            )
            row = await cur.fetchone()
            await cur.close()
        if row is None:
            return None
        return _row_to_delete(row)

    async def update_delete_job(
        self,
        job_id: str,
        *,
        status: str,
        completed_at: datetime | None = None,
        deleted_counts: dict[str, int] | None = None,
        error: str | None = None,
    ) -> None:
        async with connect(self._db_path) as conn:
            await conn.execute(
                "UPDATE delete_jobs SET status=?, completed_at=?, "
                "deleted_counts=?, error=? WHERE job_id=?",
                (
                    status,
                    _iso(completed_at) if completed_at else None,
                    json.dumps(deleted_counts or {}, sort_keys=True),
                    error,
                    job_id,
                ),
            )
            await conn.commit()

    # ---------------------------------------------- delete confirmation

    async def issue_confirm_token(self, tenant_id: str) -> tuple[str, datetime]:
        token = "dcf_" + secrets.token_urlsafe(32)
        now = _now()
        expires_at = now + timedelta(seconds=DELETE_CONFIRM_TTL_SECONDS)
        async with connect(self._db_path) as conn:
            await conn.execute(
                "INSERT INTO delete_confirm_tokens "
                "(confirm_token, tenant_id, created_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (token, tenant_id, _iso(now), _iso(expires_at)),
            )
            await conn.commit()
        return token, expires_at

    async def consume_confirm_token(
        self,
        token: str,
        tenant_id: str,
    ) -> bool:
        """Validate + delete a confirm token. True iff matched + unexpired."""

        async with connect(self._db_path) as conn:
            cur = await conn.execute(
                "SELECT tenant_id, expires_at FROM delete_confirm_tokens "
                "WHERE confirm_token = ?",
                (token,),
            )
            row = await cur.fetchone()
            await cur.close()
            if row is None:
                return False
            if row["tenant_id"] != tenant_id:
                return False
            expires_at = _parse_ts(row["expires_at"])
            if expires_at is None or expires_at < _now():
                # Stale row — cleanup but reject the call.
                await conn.execute(
                    "DELETE FROM delete_confirm_tokens WHERE confirm_token=?",
                    (token,),
                )
                await conn.commit()
                return False
            await conn.execute(
                "DELETE FROM delete_confirm_tokens WHERE confirm_token=?",
                (token,),
            )
            await conn.commit()
        return True


# ---------------------------------------------------------------------------
# Identity-side data extraction (own DB rows for export + delete)


async def emit_identity_jsonl(db_path: Path, tenant_id: str) -> list[str]:
    """Return JSONL lines describing every tenant-scoped row in identity DB.

    Tables covered: ``tenants``, ``issued_tokens``, ``tenant_quotas``,
    ``tenant_usage``. Tokens are *metadata only* (the JWT itself was
    never persisted).
    """

    out: list[str] = []
    async with connect(db_path) as conn:
        for table, type_label in (
            ("tenants", "tenant"),
            ("issued_tokens", "token"),
            ("tenant_quotas", "tenant_quota"),
            ("tenant_usage", "tenant_usage"),
        ):
            id_col = "id" if table == "tenants" else "tenant_id"
            cur = await conn.execute(
                f"SELECT * FROM {table} WHERE {id_col} = ?",
                (tenant_id,),
            )
            rows = list(await cur.fetchall())
            await cur.close()
            for row in rows:
                payload: dict[str, Any] = {"type": type_label}
                for key in row.keys():
                    payload[key] = row[key]
                out.append(json.dumps(payload, sort_keys=True, default=str))
    return out


async def delete_identity_data(db_path: Path, tenant_id: str) -> dict[str, int]:
    """Hard-delete every tenant-scoped row in the identity DB.

    Order: child rows (tokens, quotas, usage) first, then the tenants row
    itself. The default tenant (``"default"``) is preserved — deleting
    it would brick downstream services that fall back to it.
    """

    counts: dict[str, int] = {}
    async with connect(db_path) as conn:
        for table in ("issued_tokens", "tenant_quotas", "tenant_usage"):
            cur = await conn.execute(
                f"SELECT COUNT(*) AS c FROM {table} WHERE tenant_id = ?",
                (tenant_id,),
            )
            row = await cur.fetchone()
            counts[table] = int(row["c"]) if row else 0
            await cur.close()
            await conn.execute(
                f"DELETE FROM {table} WHERE tenant_id = ?",
                (tenant_id,),
            )

        if tenant_id != "default":
            cur = await conn.execute(
                "SELECT COUNT(*) AS c FROM tenants WHERE id = ?",
                (tenant_id,),
            )
            row = await cur.fetchone()
            counts["tenants"] = int(row["c"]) if row else 0
            await cur.close()
            await conn.execute(
                "DELETE FROM tenants WHERE id = ?",
                (tenant_id,),
            )
        else:
            counts["tenants"] = 0
        await conn.commit()
    return counts


# ---------------------------------------------------------------------------
# Orchestrators (background tasks)


async def run_export(
    *,
    store: ComplianceStore,
    export_id: str,
    tenant_id: str,
    workspace_url: str | None,
    gateway_url: str | None,
    exports_dir: Path,
    db_path: Path,
    http_client: httpx.AsyncClient | None = None,
) -> ExportStatus:
    """Build the ZIP for an export job and update its status.

    Returns the final :class:`ExportStatus`. Failures mark the job
    ``failed`` with an error message; the ZIP file is not created.
    """

    exports_dir.mkdir(parents=True, exist_ok=True)
    out_path = exports_dir / f"{export_id}.zip"
    own_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=30.0)
    try:
        ws_jsonl = ""
        gw_jsonl = ""
        if workspace_url:
            try:
                resp = await client.get(
                    f"{workspace_url.rstrip('/')}"
                    f"/v1/admin/tenant/{tenant_id}/export-data",
                    headers={"Authorization": "Bearer compliance-orch"},
                )
                resp.raise_for_status()
                ws_jsonl = resp.text or ""
            except (httpx.HTTPError, httpx.TimeoutException) as exc:
                # Soft-fail: identity-only exports still useful.
                ws_jsonl = ""
                _ = exc
        if gateway_url:
            try:
                resp = await client.get(
                    f"{gateway_url.rstrip('/')}"
                    f"/v1/admin/tenant/{tenant_id}/export-data",
                    headers={"Authorization": "Bearer compliance-orch"},
                )
                resp.raise_for_status()
                gw_jsonl = resp.text or ""
            except (httpx.HTTPError, httpx.TimeoutException):
                gw_jsonl = ""

        identity_lines = await emit_identity_jsonl(db_path, tenant_id)
        identity_jsonl = "\n".join(identity_lines)
        if identity_jsonl:
            identity_jsonl += "\n"

        manifest = {
            "version": 1,
            "export_id": export_id,
            "tenant_id": tenant_id,
            "generated_at": _iso(_now()),
            "expires_at": _iso(_now() + timedelta(hours=EXPORT_TTL_HOURS)),
            "files": [
                "manifest.json",
                "identity.jsonl",
                "workspace.jsonl",
                "gateway.jsonl",
            ],
        }

        # Build the ZIP in-memory then atomically write to disk.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "manifest.json",
                json.dumps(manifest, sort_keys=True, indent=2),
            )
            zf.writestr("identity.jsonl", identity_jsonl)
            zf.writestr("workspace.jsonl", ws_jsonl)
            zf.writestr("gateway.jsonl", gw_jsonl)
        body = buf.getvalue()
        out_path.write_bytes(body)
        completed = _now()
        expires = completed + timedelta(hours=EXPORT_TTL_HOURS)
        await store.update_export(
            export_id,
            status="ready",
            completed_at=completed,
            expires_at=expires,
            size_bytes=len(body),
        )
    except Exception as exc:  # noqa: BLE001
        await store.update_export(
            export_id,
            status="failed",
            completed_at=_now(),
            error=str(exc),
        )
    finally:
        if own_client:
            await client.aclose()

    final = await store.get_export(export_id)
    assert final is not None  # noqa: S101
    return final


async def run_delete(
    *,
    store: ComplianceStore,
    job_id: str,
    tenant_id: str,
    workspace_url: str | None,
    gateway_url: str | None,
    db_path: Path,
    http_client: httpx.AsyncClient | None = None,
) -> DeleteJob:
    """Execute the hard-delete cascade for a tenant.

    Order: workspace → gateway → identity. Each step is recorded in
    ``deleted_counts`` so the operator can audit exactly what got
    removed. Failures past the workspace step still progress; the job
    only transitions to ``failed`` when identity itself fails.
    """

    own_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=30.0)
    counts: dict[str, int] = {}
    try:
        await store.update_delete_job(
            job_id,
            status="in_progress",
        )

        if workspace_url:
            try:
                resp = await client.delete(
                    f"{workspace_url.rstrip('/')}"
                    f"/v1/admin/tenant/{tenant_id}/data",
                    headers={"Authorization": "Bearer compliance-orch"},
                )
                resp.raise_for_status()
                ws_counts = resp.json().get("deleted", {}) or {}
                for k, v in ws_counts.items():
                    counts[f"workspace.{k}"] = int(v)
            except (httpx.HTTPError, httpx.TimeoutException) as exc:
                counts["workspace.error"] = 0
                _ = exc

        if gateway_url:
            try:
                resp = await client.delete(
                    f"{gateway_url.rstrip('/')}"
                    f"/v1/admin/tenant/{tenant_id}/data",
                    headers={"Authorization": "Bearer compliance-orch"},
                )
                resp.raise_for_status()
                gw_counts = resp.json().get("deleted", {}) or {}
                for k, v in gw_counts.items():
                    counts[f"gateway.{k}"] = int(v)
            except (httpx.HTTPError, httpx.TimeoutException):
                counts["gateway.error"] = 0

        identity_counts = await delete_identity_data(db_path, tenant_id)
        for k, v in identity_counts.items():
            counts[f"identity.{k}"] = int(v)

        await store.update_delete_job(
            job_id,
            status="completed",
            completed_at=_now(),
            deleted_counts=counts,
        )
    except Exception as exc:  # noqa: BLE001
        await store.update_delete_job(
            job_id,
            status="failed",
            completed_at=_now(),
            deleted_counts=counts,
            error=str(exc),
        )
    finally:
        if own_client:
            await client.aclose()

    final = await store.get_delete_job(job_id)
    assert final is not None  # noqa: S101
    return final


__all__ = [
    "ComplianceStore",
    "DELETE_CONFIRM_TTL_SECONDS",
    "EXPORT_TTL_HOURS",
    "delete_identity_data",
    "emit_identity_jsonl",
    "new_delete_job_id",
    "new_export_id",
    "run_delete",
    "run_export",
]
