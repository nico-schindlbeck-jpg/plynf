# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Aggregate read-only data from the workspace + gateway services.

The overview builder fans out in parallel, tolerates partial failure, and
returns a single JSON payload the SPA renders into the dashboard.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .logging_config import get_logger
from .settings import Settings


def _now_iso() -> str:
    """Return the current UTC instant as an RFC-3339 string."""
    # ``datetime.UTC`` is 3.11+; the verify-venv runs 3.9 so we use the
    # backwards-compatible ``timezone.utc`` form here.
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")  # noqa: UP017


def _parse_dt(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp tolerant of ``Z`` suffix; ``None`` on failure."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _build_timeseries(
    events: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    minutes: int = 60,
) -> list[dict[str, Any]]:
    """Bucket recent audit events into ``minutes`` 1-minute buckets.

    The returned list is always ``minutes`` long, oldest first. Each bucket
    carries the count of events whose timestamp falls inside that minute and
    the summed cost. Empty buckets are emitted with zero counts so the SPA
    can draw a contiguous sparkline regardless of activity.
    """

    now = now or datetime.now(timezone.utc)
    # Floor to the current minute so bucket math stays clean.
    bucket_now = now.replace(second=0, microsecond=0)
    buckets: list[dict[str, Any]] = []
    counts: dict[datetime, dict[str, float]] = {}

    for evt in events or []:
        ts = _parse_dt(evt.get("timestamp"))
        if ts is None:
            continue
        bkt = ts.replace(second=0, microsecond=0)
        if bkt > bucket_now:
            continue
        if bkt < bucket_now - timedelta(minutes=minutes - 1):
            continue
        slot = counts.setdefault(bkt, {"count": 0.0, "cost_usd": 0.0})
        slot["count"] += 1
        slot["cost_usd"] += float(evt.get("cost_estimate_usd") or 0.0)

    # Fill ``minutes`` slots oldest → newest.
    for i in range(minutes - 1, -1, -1):
        bkt = bucket_now - timedelta(minutes=i)
        slot = counts.get(bkt) or {"count": 0.0, "cost_usd": 0.0}
        buckets.append(
            {
                "t": bkt.isoformat().replace("+00:00", "Z"),
                "count": int(slot["count"]),
                "cost_usd": round(float(slot["cost_usd"]), 6),
            }
        )

    return buckets


def _summarise_observability(
    status: dict[str, Any] | None,
    events: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the dashboard's ``observability`` section.

    Combines the gateway's emitter status with derived 5-minute counters from
    the audit events. Missing status (older gateway) → defaults with
    ``otlp_enabled: False`` so the SPA can render the disabled state.
    """

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=5)
    events_5min = 0
    errors_5min = 0
    for evt in events or []:
        ts = _parse_dt(evt.get("timestamp"))
        if ts is None or ts < cutoff:
            continue
        events_5min += 1
        if evt.get("error"):
            errors_5min += 1

    base: dict[str, Any] = {
        "otlp_enabled": False,
        "otlp_endpoint": None,
        "events_emitted": 0,
        "last_emit_at": None,
        "flush_errors": 0,
        "events_emitted_5min": events_5min,
        "errors_5min": errors_5min,
    }
    if status:
        base["otlp_enabled"] = bool(status.get("otlp_enabled", False))
        base["otlp_endpoint"] = status.get("otlp_endpoint")
        base["events_emitted"] = int(status.get("events_emitted") or 0)
        base["last_emit_at"] = status.get("last_emit_at")
        base["flush_errors"] = int(status.get("flush_errors") or 0)
    return base


class OverviewBuilder:
    """Fan-out aggregator for ``/api/overview``.

    The builder holds a long-lived ``httpx.AsyncClient`` for connection reuse.
    Every backend call is wrapped in :meth:`_safe_get` so a partial outage
    degrades gracefully — the response carries ``partial: true`` and any
    available data still flows through.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.backend_timeout_seconds),
            headers={"Authorization": settings.auth_header},
        )
        self._log = get_logger("dashboard.overview")

    @property
    def client(self) -> httpx.AsyncClient:
        """The underlying httpx client (for proxy-style endpoints)."""
        return self._client

    async def aclose(self) -> None:
        """Close the underlying httpx client (only if we own it)."""
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------ public

    async def build(self) -> dict[str, Any]:
        """Build the dashboard overview payload by fanning out in parallel.

        Returns a dict with the shape:

        ``{services, workspaces, audit, cache, tools, tenants, partial, fetched_at}``

        where ``services`` carries health/version for each backend (now
        including the identity service) and ``audit`` is a summary of recent
        calls. ``partial`` is True if any upstream call failed.
        """
        ws_url = self._settings.workspace_url.rstrip("/")
        gw_url = self._settings.gateway_url.rstrip("/")

        # Wave 1: probe healthz of all four services + read top-level
        # collections from workspace and gateway in parallel.
        services_task = self._service_status()
        workspaces_task = self._safe_get(f"{ws_url}/v1/workspaces")
        audit_stats_task = self._safe_get(f"{gw_url}/v1/audit/stats")
        cache_stats_task = self._safe_get(f"{gw_url}/v1/cache/stats")
        tools_task = self._safe_get(f"{gw_url}/v1/tools")
        ws_tenants_task = self._safe_get(f"{ws_url}/v1/tenants")
        gw_tenants_task = self._safe_get(f"{gw_url}/v1/tenants")

        # v0.4: OTLP emitter status + recent events for the time-series graph.
        # Both endpoints can fail silently (older gateway, partial outage) —
        # ``_safe_get`` returns ``ok=False`` and we fall back to defaults.
        otlp_status_task = self._safe_get(f"{gw_url}/v1/observability/status")
        recent_audit_task = self._safe_get(
            f"{gw_url}/v1/audit", params={"limit": 1000}
        )

        (
            services,
            ws_resp,
            audit_stats_resp,
            cache_resp,
            tools_resp,
            ws_tenants_resp,
            gw_tenants_resp,
            otlp_status_resp,
            recent_audit_resp,
        ) = await asyncio.gather(
            services_task,
            workspaces_task,
            audit_stats_task,
            cache_stats_task,
            tools_task,
            ws_tenants_task,
            gw_tenants_task,
            otlp_status_task,
            recent_audit_task,
        )

        partial = any(
            not r["ok"]
            for r in (ws_resp, audit_stats_resp, cache_resp, tools_resp)
        )

        workspaces_raw: list[dict[str, Any]] = []
        if ws_resp["ok"]:
            workspaces_raw = (ws_resp["data"] or {}).get("workspaces", []) or []

        # Workspaces summary — keep it minimal: id, name, created_at, tenant_id.
        ws_list = [
            {
                "id": w.get("id"),
                "name": w.get("name") or w.get("id"),
                "tenant_id": w.get("tenant_id") or "default",
                "created_at": w.get("created_at"),
                "updated_at": w.get("updated_at"),
            }
            for w in workspaces_raw
        ]

        audit_stats = {}
        if audit_stats_resp["ok"]:
            audit_stats = (audit_stats_resp["data"] or {}).get("stats", {}) or {}

        cache = cache_resp["data"] if cache_resp["ok"] else {}
        tools = (tools_resp["data"] or {}).get("tools", []) if tools_resp["ok"] else []

        audit_summary = {
            "total_invocations": int(audit_stats.get("total_invocations") or 0),
            "cached_count": int(audit_stats.get("cached_count") or 0),
            "error_count": int(audit_stats.get("error_count") or 0),
            "total_cost_usd": float(audit_stats.get("total_cost_usd") or 0.0),
            "by_tool": list(audit_stats.get("by_tool") or []),
        }

        cache_summary = {
            "hits": int((cache or {}).get("hits") or 0),
            "misses": int((cache or {}).get("misses") or 0),
            "entries": int((cache or {}).get("entries") or 0),
            "size_bytes": int((cache or {}).get("size_bytes") or 0),
        }

        tenants = _merge_tenants(
            (ws_tenants_resp["data"] or {}).get("tenants") if ws_tenants_resp["ok"] else None,
            (gw_tenants_resp["data"] or {}).get("tenants") if gw_tenants_resp["ok"] else None,
        )

        # v0.4 — observability + per-minute time series.
        # The audit listing might fail (older gateway, partial outage); when
        # it does we fall back to an empty list so the graph still renders 60
        # zero buckets.
        recent_events: list[dict[str, Any]] = []
        if recent_audit_resp["ok"]:
            recent_events = (recent_audit_resp["data"] or {}).get("events") or []

        otlp_status: dict[str, Any] | None = None
        if otlp_status_resp["ok"]:
            otlp_status = otlp_status_resp["data"] or None

        observability = _summarise_observability(otlp_status, recent_events)
        timeseries = {
            "tool_calls_per_minute": _build_timeseries(recent_events, minutes=60),
        }

        # v0.5 — enumerate dead-letter queues across all visible workspaces.
        # We fan out a second wave (one ``GET /channels`` + per-channel DLQ
        # peek) but skip everything when there are no workspaces. Failures
        # are logged and degrade silently — we'd rather render the rest of
        # the overview than block on a slow workspace.
        deadletters: list[dict[str, Any]] = []
        if ws_list:
            deadletters = await self._collect_deadletters(ws_url, ws_list)

        return {
            "services": services,
            "workspaces": {"count": len(ws_list), "list": ws_list},
            "audit": audit_summary,
            "cache": cache_summary,
            "tools": {"count": len(tools)},
            "tenants": {"count": len(tenants), "list": tenants},
            "observability": observability,
            "timeseries": timeseries,
            "deadletters": deadletters,
            "partial": partial,
            "fetched_at": _now_iso(),
        }

    # ------------------------------------------------------------------ helpers

    async def _service_status(self) -> dict[str, dict[str, Any]]:
        """Probe ``/healthz`` for each known backend.

        Returns a dict keyed by service name with ``status`` (``up``/``down``),
        ``version`` (when reachable) and the configured ``url``.
        """
        targets = {
            "workspace": self._settings.workspace_url,
            "gateway": self._settings.gateway_url,
            "mock_mcp": self._settings.mock_mcp_url,
            "identity": self._settings.identity_url,
        }
        results = await asyncio.gather(
            *(self._healthz(name, url) for name, url in targets.items())
        )
        # ``strict=`` keyword is 3.10+; the verify-venv runs 3.9 so we omit
        # it. Lengths are equal by construction (one result per target).
        return dict(zip(targets.keys(), results))  # noqa: B905

    async def _healthz(self, name: str, base_url: str) -> dict[str, Any]:
        url = f"{base_url.rstrip('/')}/healthz"
        try:
            r = await self._client.get(url, timeout=2.0)
        except httpx.HTTPError as exc:
            self._log.warning("dashboard.healthz.fail", service=name, error=str(exc))
            return {"status": "down", "url": base_url, "error": str(exc)}
        if r.status_code != 200:
            return {"status": "down", "url": base_url, "error": f"HTTP {r.status_code}"}
        try:
            data = r.json()
        except ValueError:
            data = {}
        return {
            "status": "up",
            "url": base_url,
            "version": data.get("version"),
            "service": data.get("service") or name,
        }

    async def _collect_deadletters(
        self,
        ws_url: str,
        ws_list: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Probe DLQs for every channel of every workspace.

        For each workspace we list its channels (excluding any DLQ
        sub-channels — the workspace already filters those out) and then
        peek the deadletter endpoint for each. We only return entries with
        a non-zero count so the dashboard's panel stays terse.

        Failure modes:
        - workspace channel listing fails → that workspace contributes nothing
        - per-channel DLQ peek fails → that channel contributes nothing
        Both are logged at WARNING but never fail the overall ``build()``.
        """

        # Step 1: fan out one ``/channels`` call per workspace.
        channel_results = await asyncio.gather(
            *(
                self._safe_get(f"{ws_url}/v1/workspaces/{w['id']}/channels")
                for w in ws_list
            )
        )

        # Step 2: build the (ws_id, channel) tuples we'll query for DLQ.
        probe_pairs: list[tuple[str, str]] = []
        for ws, ch_resp in zip(ws_list, channel_results):  # noqa: B905
            if not ch_resp["ok"]:
                continue
            channels = (ch_resp["data"] or {}).get("channels") or []
            for ch in channels:
                name = ch.get("name")
                if not name or name.endswith(".deadletter"):
                    continue
                probe_pairs.append((ws["id"], name))

        if not probe_pairs:
            return []

        # Step 3: peek every DLQ in parallel. ``limit=1`` keeps the wire
        # cheap; we only care that there's *some* DLQ entry. The full list
        # comes from the per-channel UI.
        dlq_results = await asyncio.gather(
            *(
                self._safe_get(
                    f"{ws_url}/v1/workspaces/{ws_id}/channels/{name}/deadletter",
                    params={"limit": 1000},
                )
                for ws_id, name in probe_pairs
            )
        )

        out: list[dict[str, Any]] = []
        for (ws_id, name), dlq in zip(probe_pairs, dlq_results):  # noqa: B905
            if not dlq["ok"]:
                continue
            messages = (dlq["data"] or {}).get("messages") or []
            count = len(messages)
            if count <= 0:
                continue
            out.append(
                {
                    "workspace_id": ws_id,
                    "channel": name,
                    "deadletter_count": count,
                }
            )
        return out

    async def _safe_get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Wrap a GET in error handling so callers can keep going on failure.

        Returns ``{"ok": bool, "data": dict|None, "error": str|None, "status": int|None}``.
        """
        try:
            r = await self._client.get(url, params=params)
        except httpx.HTTPError as exc:
            self._log.warning("dashboard.fetch.fail", url=url, error=str(exc))
            return {"ok": False, "data": None, "error": str(exc), "status": None}
        except Exception as exc:  # pragma: no cover - defensive
            # ``respx`` raises ``AllMockedAssertionError`` (an AssertionError
            # subclass, not an httpx error) when a test forgets to mock a
            # route. We treat it the same as an unreachable upstream so a
            # missing mock degrades the overview gracefully rather than
            # 500-ing the whole endpoint.
            self._log.warning("dashboard.fetch.error", url=url, error=str(exc))
            return {"ok": False, "data": None, "error": str(exc), "status": None}
        if r.status_code >= 400:
            self._log.warning("dashboard.fetch.status", url=url, status=r.status_code)
            return {
                "ok": False,
                "data": None,
                "error": f"HTTP {r.status_code}",
                "status": r.status_code,
            }
        try:
            data = r.json()
        except ValueError as exc:
            return {
                "ok": False,
                "data": None,
                "error": str(exc),
                "status": r.status_code,
            }
        return {"ok": True, "data": data, "error": None, "status": r.status_code}


def _merge_tenants(
    workspace_tenants: list[dict[str, Any]] | None,
    gateway_tenants: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Combine tenant rows from both backends into one ID-keyed list.

    Each backend returns a slightly different shape (workspace knows about
    workspace_count, gateway about audit_count + tool_count). We outer-join
    them so the dashboard sees one row per tenant with all the available
    counters.
    """

    merged: dict[str, dict[str, Any]] = {}
    for entry in workspace_tenants or []:
        tid = entry.get("id") or "default"
        merged.setdefault(tid, {"id": tid})
        merged[tid]["workspace_count"] = int(entry.get("workspace_count") or 0)
    for entry in gateway_tenants or []:
        tid = entry.get("id") or "default"
        merged.setdefault(tid, {"id": tid})
        merged[tid]["audit_count"] = int(entry.get("audit_count") or 0)
        merged[tid]["tool_count"] = int(entry.get("tool_count") or 0)

    # Fill missing counters with zero so the SPA always sees a number.
    for entry in merged.values():
        entry.setdefault("workspace_count", 0)
        entry.setdefault("audit_count", 0)
        entry.setdefault("tool_count", 0)

    return sorted(merged.values(), key=lambda d: d["id"])


__all__ = [
    "OverviewBuilder",
    "_build_timeseries",
    "_summarise_observability",
]
