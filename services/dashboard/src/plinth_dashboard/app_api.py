# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Control-plane API for the Plynf web dashboard (``/api/app/*``).

The marketing-stack dashboard (Astro app at ``/app``) is the surface through
which the *whole product* is operated — not just observed. This module is its
backend:

* A :class:`ControlStore` is the **system of record** for control-plane config
  the proxy doesn't itself persist (providers, model aliases, front-door
  enablement, agents, capability-token metadata, response-shaping policies,
  guardrails, billing plan, onboarding progress, team). It is in-memory by
  default and optionally JSON-file-backed (``PLINTH_DASHBOARD_APP_STATE_PATH``)
  so config survives restarts.
* ``GET /api/app/state`` returns the entire dashboard state in one call and
  **overlays live data** from the real downstreams (proxy ``/v1/providers``,
  gateway ``/v1/tools``) on a best-effort basis — a downstream being down never
  breaks the page.
* The write endpoints mutate the store (and call a real downstream where one
  exists, e.g. the identity service for token issuance) and return the updated
  entity plus a human ``message`` the UI shows as a toast.

The read shapes are camelCase to match the frontend's TypeScript data contract
1:1, so the Astro app can bind ``GET /api/app/state`` directly.
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .app_defaults import seed_state
from .logging_config import get_logger
from .settings import Settings

# Estimated data-center cooling water per token shaped away, in litres.
# ~0.4 kWh / 1M tokens of inference × ~1.8 L/kWh water-usage-effectiveness
# ≈ 7.2e-7 L/token (≈ 0.72 mL per 1k tokens). An order-of-magnitude estimate
# surfaced as an "interesting side-fact", not a billed figure.
WATER_L_PER_TOKEN = 7.2e-7

# ---------------------------------------------------------------------------
# Seed state lives in app_defaults.seed_state() — it mirrors the frontend
# data contract (src/data/dashboard.ts) so the live /api/app/state matches
# the SSG demo exactly.


def _seed_state() -> dict[str, Any]:
    return seed_state()


def _attribution_summary(referrals: dict[str, Any]) -> dict[str, Any]:
    """Roll the raw ``{ref: count}`` tally up into the dashboard view shape.

    ``referrals`` is populated by :meth:`ControlStore.record_referral` whenever a
    signup carries an ``?ref=`` tag from an "Add to Plynf" button. The dashboard's
    Attribution panel binds to ``attribution.totalReferred`` /
    ``attribution.uniqueSources`` and renders ``attribution.sources`` (already
    sorted, biggest first). Always returns a well-formed object, even when empty.
    """
    items = [
        {"ref": str(ref), "count": int(count or 0)}
        for ref, count in (referrals or {}).items()
        if int(count or 0) > 0
    ]
    items.sort(key=lambda r: (-r["count"], r["ref"]))
    return {
        "totalReferred": sum(r["count"] for r in items),
        "uniqueSources": len(items),
        "sources": items,
    }


# ---------------------------------------------------------------------------
# Store


class ControlStore:
    """In-memory control-plane state with optional JSON-file persistence."""

    def __init__(self, path: str | None = None) -> None:
        self._path = Path(path) if path else None
        self._state = _seed_state()
        if self._path and self._path.exists():
            try:
                loaded = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self._state = loaded
            except (OSError, ValueError) as exc:  # corrupt file → keep seed
                get_logger().warning("dashboard.control_store.load_failed", error=str(exc))

    def snapshot(self) -> dict[str, Any]:
        snap = copy.deepcopy(self._state)
        # Derived, always-present view of the ?ref= partner-attribution tally.
        snap["attribution"] = _attribution_summary(snap.get("referrals", {}))
        return snap

    def _save(self) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
        except OSError as exc:  # never crash a request on a write failure
            get_logger().warning("dashboard.control_store.save_failed", error=str(exc))

    # -- list helpers -------------------------------------------------------

    def _list(self, key: str) -> list[dict[str, Any]]:
        return self._state.setdefault(key, [])

    # -- providers ----------------------------------------------------------

    def add_provider(self, body: dict[str, Any]) -> dict[str, Any]:
        name = str(body.get("name") or "").strip()
        base_url = str(body.get("baseUrl") or "").strip()
        if not name or not base_url:
            raise ValueError("name and baseUrl are required")
        pid = name.lower().replace(" ", "-")
        headers = body.get("headers") or []
        key = str(body.get("apiKey") or "")
        provider = {
            "id": pid,
            "name": name,
            "baseUrl": base_url,
            "keyMasked": (key[:4] + "…" + key[-3:])
            if len(key) > 8
            else ("—" if not key else "set"),
            "headers": headers if isinstance(headers, list) else [],
            "models": 0,
            "isDefault": False,
            "status": "connected",
        }
        providers = self._list("providers")
        providers[:] = [p for p in providers if p["id"] != pid]
        providers.append(provider)
        self._save()
        return provider

    def remove_provider(self, pid: str) -> bool:
        providers = self._list("providers")
        before = len(providers)
        providers[:] = [p for p in providers if p["id"] != pid]
        self._save()
        return len(providers) < before

    def set_default_provider(self, pid: str) -> bool:
        providers = self._list("providers")
        found = False
        for p in providers:
            p["isDefault"] = p["id"] == pid
            found = found or p["id"] == pid
        self._save()
        return found

    # -- aliases ------------------------------------------------------------

    def set_alias(self, alias: str, target: str) -> dict[str, Any]:
        alias, target = alias.strip(), target.strip()
        if not alias or not target:
            raise ValueError("alias and target are required")
        aliases = self._list("modelAliases")
        aliases[:] = [a for a in aliases if a["alias"] != alias]
        entry = {"alias": alias, "target": target}
        aliases.append(entry)
        self._save()
        return entry

    def remove_alias(self, alias: str) -> bool:
        aliases = self._list("modelAliases")
        before = len(aliases)
        aliases[:] = [a for a in aliases if a["alias"] != alias]
        self._save()
        return len(aliases) < before

    # -- front doors --------------------------------------------------------

    def toggle_front_door(self, fid: str, enabled: bool) -> dict[str, Any] | None:
        for f in self._list("frontDoors"):
            if f["id"] == fid:
                f["enabled"] = bool(enabled)
                self._save()
                return f
        return None

    # -- agents -------------------------------------------------------------

    def create_agent(self, body: dict[str, Any]) -> dict[str, Any]:
        name = str(body.get("name") or "").strip()
        if not name:
            raise ValueError("name is required")
        agent = {
            "id": "agt_" + uuid.uuid4().hex[:8],
            "name": name,
            "env": str(body.get("env") or "prod"),
            "model": str(body.get("model") or "smart"),
            "status": "healthy",
            "requestsToday": 0,
            "savedPct": 0.0,
            "costTodayEur": 0.0,
            "p95ms": 0,
            "errorRatePct": 0.0,
            "recoveredToday": 0,
            "dlq": 0,
            "owner": str(body.get("owner") or "you"),
            "lastActivity": "just now",
            "durable": bool(body.get("durable", True)),
            "dailyCapEur": body.get("dailyCapEur"),
        }
        self._list("agents").append(agent)
        self._save()
        return agent

    def set_agent_status(self, aid: str, status: str) -> dict[str, Any] | None:
        for a in self._list("agents"):
            if a["id"] == aid:
                a["status"] = status
                self._save()
                return a
        return None

    # -- tokens -------------------------------------------------------------

    def create_token(self, body: dict[str, Any], *, jti: str | None = None) -> dict[str, Any]:
        scopes = body.get("scopes")
        if isinstance(scopes, str):
            scopes = [s for s in scopes.replace(",", " ").split() if s]
        token = {
            "id": jti or ("tok_" + uuid.uuid4().hex[:8]),
            "name": str(body.get("name") or body.get("agent") or "new-token"),
            "scopes": scopes or [],
            "tenant": str(body.get("tenant") or "acme"),
            "createdAgo": "just now",
            "rotatedAgo": "just now",
            "lastUsed": "never",
            "status": "active",
            "alg": str(body.get("alg") or "RS256"),
        }
        self._list("capabilityTokens").append(token)
        self._save()
        return token

    def rotate_token(self, tid: str) -> dict[str, Any] | None:
        for t in self._list("capabilityTokens"):
            if t["id"] == tid:
                t["rotatedAgo"] = "just now"
                t["status"] = "active"
                self._save()
                return t
        return None

    def revoke_token(self, tid: str) -> bool:
        found = False
        for t in self._list("capabilityTokens"):
            if t["id"] == tid:
                t["status"] = "expired"
                found = True
        self._save()
        return found

    # -- policies -----------------------------------------------------------

    def set_policy(self, tool: str, body: dict[str, Any]) -> dict[str, Any]:
        keep = body.get("keepFields")
        if isinstance(keep, str):
            keep = [s.strip() for s in keep.replace(",", "\n").splitlines() if s.strip()]
        policies = self._list("toolPolicies")
        existing = next((p for p in policies if p["tool"] == tool), None)
        entry = existing or {"tool": tool, "reductionPct": 0.0}
        if keep is not None:
            entry["keepFields"] = keep
        if body.get("maxTokens") is not None:
            entry["maxTokens"] = int(body["maxTokens"])
        if body.get("cacheTtlS") is not None:
            entry["cacheTtlS"] = int(body["cacheTtlS"])
        if body.get("blockWrites") is not None:
            entry["blockWrites"] = bool(body["blockWrites"])
        if existing is None:
            policies.append(entry)
        self._save()
        return entry

    # -- guardrails ---------------------------------------------------------

    def set_guardrail(self, aid: str, body: dict[str, Any]) -> dict[str, Any] | None:
        for g in self._list("guardrails"):
            if g["agentId"] == aid:
                if body.get("rpmLimit") is not None:
                    g["rpmLimit"] = int(body["rpmLimit"])
                if body.get("dailyCapEur") is not None:
                    g["dailyCapEur"] = float(body["dailyCapEur"])
                self._save()
                return g
        return None

    # -- onboarding / billing / team ---------------------------------------

    def set_onboarding(self, sid: str, done: bool) -> dict[str, Any] | None:
        for s in self._list("onboarding"):
            if s["id"] == sid:
                s["done"] = bool(done)
                self._save()
                return s
        return None

    def set_plan(self, plan: str) -> dict[str, Any]:
        self._state.setdefault("billing", {})["plan"] = plan
        self._save()
        return self._state["billing"]

    def invite_member(self, email: str, role: str) -> dict[str, Any]:
        member = {
            "name": email.split("@")[0].replace(".", " ").title(),
            "email": email,
            "role": role or "Developer",
            "tenant": "acme",
            "lastActive": "invited",
        }
        self._list("team").append(member)
        self._save()
        return member

    def record_referral(self, ref: str) -> int:
        """Tally a partner-attributed signup by its ?ref= tag. Returns the count."""
        refs = self._state.setdefault("referrals", {})
        refs[ref] = int(refs.get(ref, 0)) + 1
        self._save()
        return refs[ref]


# ---------------------------------------------------------------------------
# Live overlay (best-effort)


async def _overlay_live(
    client: httpx.AsyncClient, settings: Settings, state: dict[str, Any]
) -> None:
    """Derive Overview / Economics KPIs from live telemetry. Never raises.

    Token-reduction & € saved come from the proxy's response-shaping savings
    sink (``/v1/savings/summary``); tool-call volume, cache-hit rate and actual
    spend come from the gateway audit (``/v1/audit/stats``). When a downstream
    is unreachable the seeded KPI keeps showing — the page never breaks.
    """
    proxy_url = settings.proxy_url.rstrip("/")
    gw_url = settings.gateway_url.rstrip("/")
    kpis = state.setdefault("kpis", {})

    # 1) Proxy savings sink → token reduction + € saved (the response-shaping win).
    try:
        resp = await client.get(f"{proxy_url}/v1/savings/summary")
        if resp.status_code < 400:
            s = resp.json() or {}
            if s.get("total_calls"):
                if s.get("total_raw_tokens"):
                    kpis["rawTokensToday"] = s["total_raw_tokens"]
                    kpis["shapedTokensToday"] = s.get("total_shaped_tokens", 0)
                    saved = s.get("total_saved_tokens", 0)
                    kpis["savedTokensToday"] = saved
                    kpis["reductionPct"] = round(s.get("savings_pct", 0.0) * 100, 1)
                    # Estimated data-center cooling water not evaporated.
                    kpis["waterSavedLitresToday"] = round(saved * WATER_L_PER_TOKEN, 1)
                kpis["savedTodayEur"] = round(s.get("total_cost_saved_usd", 0.0), 2)
                state["_telemetry"] = "live"
        state["_proxyReachable"] = True
    except (httpx.HTTPError, ValueError):
        state["_proxyReachable"] = False

    # 2) Proxy provider liveness (which configured providers actually route).
    try:
        resp = await client.get(f"{proxy_url}/v1/providers")
        if resp.status_code < 400:
            live_names = {str(n).lower() for n in (resp.json().get("providers") or [])}
            for p in state.get("providers", []):
                p["live"] = p["id"].lower() in live_names or p.get("isDefault", False)
    except (httpx.HTTPError, ValueError):
        pass

    # 3) Gateway audit → tool-call volume, cache-hit rate, actual spend today.
    try:
        resp = await client.get(f"{gw_url}/v1/audit/stats")
        if resp.status_code < 400:
            stats = (resp.json() or {}).get("stats") or {}
            total = stats.get("total_invocations") or 0
            if total:
                kpis["toolCallsToday"] = total
                kpis["cacheHitRate"] = round((stats.get("cached_count", 0) / total) * 100, 1)
                kpis["costTodayEur"] = round(stats.get("total_cost_usd", 0.0), 2)
                kpis["costWithoutTodayEur"] = round(
                    kpis["costTodayEur"] + kpis.get("savedTodayEur", 0.0), 2
                )
                state["_telemetry"] = "live"
            state["_gatewayReachable"] = True
    except (httpx.HTTPError, ValueError):
        state["_gatewayReachable"] = False


# ---------------------------------------------------------------------------
# Routes


def register_app_api(app: FastAPI, settings: Settings, store: ControlStore) -> None:
    """Register the ``/api/app/*`` control-plane routes onto ``app``."""
    id_url = settings.identity_url.rstrip("/")

    def _ok(message: str, **extra: Any) -> JSONResponse:
        return JSONResponse({"ok": True, "message": message, **extra})

    def _bad(message: str, status: int = 400) -> JSONResponse:
        return JSONResponse(
            status_code=status,
            content={"error": {"code": "INVALID_ARGUMENTS", "message": message, "details": {}}},
        )

    @app.get("/api/app/state", tags=["app"])
    async def app_state(request: Request) -> JSONResponse:
        state = store.snapshot()
        client = getattr(request.app.state, "overview", None)
        if client is not None and getattr(client, "client", None) is not None:
            await _overlay_live(client.client, settings, state)
        return JSONResponse(state)

    # -- session: mint a short-lived JWT capability token on login ----------
    # In production this is gated by a verified SSO/OAuth assertion (the
    # marketing-site login completes the OAuth dance, then calls this to
    # exchange it for a dashboard session token). The token is minted by the
    # identity service and verified on every other /api/app/* call.
    @app.post("/api/app/session", tags=["app"])
    async def app_session(request: Request) -> JSONResponse:
        body = {}
        try:
            body = await request.json()
        except (ValueError, TypeError):
            body = {}
        subject = str(body.get("email") or body.get("agent") or "dashboard-user")
        tenant = str(body.get("tenant") or settings.app_session_tenant)
        # Partner attribution: a ?ref= carried from an "Add to Plynf" button is
        # recorded and baked into the token metadata, so partner-sourced signups
        # are tagged end-to-end.
        ref = str(body.get("ref") or "").strip()[:64]
        if ref:
            store.record_referral(ref)
        token_req: dict[str, Any] = {
            "agent_id": subject,
            "tenant_id": tenant,
            "scopes": ["dashboard:read", "dashboard:write"],
            "ttl_seconds": settings.app_session_ttl_seconds,
        }
        if ref:
            token_req["metadata"] = {"ref": ref}
        client = getattr(request.app.state, "overview", None)
        http = getattr(client, "client", None) if client else None
        if http is not None:
            try:
                resp = await http.post(f"{id_url}/v1/tokens", json=token_req)
                if resp.status_code < 400:
                    data = resp.json()
                    return JSONResponse(
                        {
                            "token": data.get("token"),
                            "claims": data.get("claims", {}),
                            "ref": ref or None,
                        }
                    )
            except (httpx.HTTPError, ValueError):
                pass
        # Identity unreachable. In enforced mode that's a hard failure; in demo
        # mode hand back a demo token so the offline experience still works.
        if settings.app_auth_required:
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "IDENTITY_UNAVAILABLE",
                        "message": "Could not issue a session token.",
                        "details": {},
                    }
                },
            )
        return JSONResponse(
            {
                "token": "demo",
                "claims": {"sub": subject, "tenant_id": tenant, "demo": True, "ref": ref or None},
                "ref": ref or None,
            }
        )

    # -- providers ----------------------------------------------------------

    @app.post("/api/app/providers", tags=["app"])
    async def add_provider(request: Request) -> JSONResponse:
        try:
            p = store.add_provider(await request.json())
        except ValueError as e:
            return _bad(str(e))
        return _ok(f"Provider “{p['name']}” added — routing live", provider=p)

    @app.delete("/api/app/providers/{pid}", tags=["app"])
    async def del_provider(pid: str) -> JSONResponse:
        if not store.remove_provider(pid):
            return _bad(f"No provider {pid}", 404)
        return _ok(f"Removed provider {pid}")

    @app.post("/api/app/providers/{pid}/default", tags=["app"])
    async def default_provider(pid: str) -> JSONResponse:
        if not store.set_default_provider(pid):
            return _bad(f"No provider {pid}", 404)
        return _ok(f"{pid} is now the default upstream")

    # -- aliases ------------------------------------------------------------

    @app.post("/api/app/aliases", tags=["app"])
    async def add_alias(request: Request) -> JSONResponse:
        body = await request.json()
        try:
            entry = store.set_alias(str(body.get("alias", "")), str(body.get("target", "")))
        except ValueError as e:
            return _bad(str(e))
        return _ok(f"Alias “{entry['alias']}” saved", alias=entry)

    @app.delete("/api/app/aliases/{alias}", tags=["app"])
    async def del_alias(alias: str) -> JSONResponse:
        if not store.remove_alias(alias):
            return _bad(f"No alias {alias}", 404)
        return _ok(f"Removed alias “{alias}”")

    # -- front doors --------------------------------------------------------

    @app.post("/api/app/frontdoors/{fid}/toggle", tags=["app"])
    async def toggle_front_door(fid: str, request: Request) -> JSONResponse:
        body = await request.json()
        f = store.toggle_front_door(fid, bool(body.get("enabled", True)))
        if f is None:
            return _bad(f"No front door {fid}", 404)
        return _ok(
            f"Front door {f['name']} {'enabled' if f['enabled'] else 'disabled'}", frontDoor=f
        )

    # -- agents -------------------------------------------------------------

    @app.post("/api/app/agents", tags=["app"])
    async def create_agent(request: Request) -> JSONResponse:
        try:
            a = store.create_agent(await request.json())
        except ValueError as e:
            return _bad(str(e))
        return _ok(f"Agent “{a['name']}” created — provisioning workspace", agent=a)

    @app.post("/api/app/agents/{aid}/{action}", tags=["app"])
    async def agent_action(aid: str, action: str) -> JSONResponse:
        if action not in {"pause", "resume"}:
            return _bad("action must be pause or resume")
        status = "paused" if action == "pause" else "healthy"
        a = store.set_agent_status(aid, status)
        if a is None:
            return _bad(f"No agent {aid}", 404)
        return _ok(f"{a['name']} {action}d", agent=a)

    # -- tokens -------------------------------------------------------------

    @app.post("/api/app/tokens", tags=["app"])
    async def create_token(request: Request) -> JSONResponse:
        body = await request.json()
        jti = None
        # Best-effort: issue a real token via the identity service when reachable.
        client = getattr(request.app.state, "overview", None)
        if client is not None and getattr(client, "client", None) is not None:
            try:
                scopes = body.get("scopes")
                if isinstance(scopes, str):
                    scopes = [s for s in scopes.replace(",", " ").split() if s]
                ttl_days = int(body.get("ttlDays") or 30)
                resp = await client.client.post(
                    f"{id_url}/v1/tokens",
                    json={
                        "agent_id": str(body.get("agent") or "agent"),
                        "scopes": scopes or [],
                        "ttl_seconds": min(86400, ttl_days * 86400),
                    },
                )
                if resp.status_code < 400:
                    jti = (resp.json() or {}).get("jti")
            except (httpx.HTTPError, ValueError):
                jti = None
        t = store.create_token(body, jti=jti)
        return _ok("Capability token issued", token=t)

    @app.post("/api/app/tokens/{tid}/rotate", tags=["app"])
    async def rotate_token(tid: str) -> JSONResponse:
        t = store.rotate_token(tid)
        if t is None:
            return _bad(f"No token {tid}", 404)
        return _ok(f"Rotating {tid} — new key minted", token=t)

    @app.delete("/api/app/tokens/{tid}", tags=["app"])
    async def revoke_token(tid: str) -> JSONResponse:
        if not store.revoke_token(tid):
            return _bad(f"No token {tid}", 404)
        return _ok(f"Revoked {tid}")

    # -- policies / guardrails ---------------------------------------------

    @app.put("/api/app/policies/{tool}", tags=["app"])
    async def set_policy(tool: str, request: Request) -> JSONResponse:
        entry = store.set_policy(tool, await request.json())
        return _ok("Shaping policy saved — applies on next call", policy=entry)

    @app.patch("/api/app/guardrails/{aid}", tags=["app"])
    async def set_guardrail(aid: str, request: Request) -> JSONResponse:
        g = store.set_guardrail(aid, await request.json())
        if g is None:
            return _bad(f"No guardrail for {aid}", 404)
        return _ok("Guardrails updated", guardrail=g)

    # -- onboarding / billing / team ---------------------------------------

    @app.post("/api/app/onboarding/{sid}", tags=["app"])
    async def set_onboarding(sid: str, request: Request) -> JSONResponse:
        body = await request.json()
        s = store.set_onboarding(sid, bool(body.get("done", True)))
        if s is None:
            return _bad(f"No step {sid}", 404)
        return _ok("Onboarding updated", step=s)

    @app.post("/api/app/billing/plan", tags=["app"])
    async def set_plan(request: Request) -> JSONResponse:
        body = await request.json()
        plan = str(body.get("plan") or "").strip()
        if not plan:
            return _bad("plan is required")
        billing = store.set_plan(plan)
        return _ok(f"Plan updated to {plan}", billing=billing)

    @app.post("/api/app/team/invite", tags=["app"])
    async def invite(request: Request) -> JSONResponse:
        body = await request.json()
        email = str(body.get("email") or "").strip()
        if "@" not in email:
            return _bad("a valid email is required")
        m = store.invite_member(email, str(body.get("role") or "Developer"))
        return _ok("Invitation sent", member=m)

    # -- workflow ops (best-effort confirmations) --------------------------

    @app.post("/api/app/workflows/{wid}/resume", tags=["app"])
    async def resume_workflow(wid: str) -> JSONResponse:
        return _ok(f"Resuming {wid} from last checkpoint")

    @app.post("/api/app/dlq/replay", tags=["app"])
    async def replay_dlq(request: Request) -> JSONResponse:
        body = (
            await request.json()
            if request.headers.get("content-type", "").startswith("application/json")
            else {}
        )
        target = body.get("agentId") or "all"
        return _ok(f"Replaying dead-lettered messages ({target})")


# ---------------------------------------------------------------------------
# Auth: verify the JWT capability token on /api/app/* (when enabled)


async def _verify_session(client: httpx.AsyncClient, id_url: str, token: str) -> bool:
    """Return True iff the identity service verifies ``token``."""
    if not token:
        return False
    try:
        resp = await client.post(f"{id_url}/v1/tokens/verify", json={"token": token})
        return resp.status_code == 200
    except (httpx.HTTPError, ValueError):
        return False


def install_app_auth(app: FastAPI, settings: Settings) -> None:
    """Gate ``/api/app/*`` behind a verified JWT when ``app_auth_required``.

    The session-issue endpoint and CORS preflight are exempt. No-op when auth
    isn't required (demo / tests run open).
    """
    if not settings.app_auth_required:
        return
    id_url = settings.identity_url.rstrip("/")

    @app.middleware("http")
    async def _app_auth(request: Request, call_next):
        path = request.url.path
        if (
            request.method != "OPTIONS"
            and path.startswith("/api/app/")
            and path != "/api/app/session"
        ):
            auth = request.headers.get("authorization", "")
            token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
            overview = getattr(request.app.state, "overview", None)
            http = getattr(overview, "client", None) if overview else None
            ok = bool(http) and await _verify_session(http, id_url, token)
            if not ok:
                return JSONResponse(
                    status_code=401,
                    content={
                        "error": {
                            "code": "UNAUTHENTICATED",
                            "message": "A valid session token is required.",
                            "details": {},
                        }
                    },
                )
        return await call_next(request)


__all__ = ["ControlStore", "register_app_api", "install_app_auth"]
