# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""FastAPI app: OpenAI-compatible /v1/chat/completions with tool-call interception.

Flow on a single request:

  1. Authenticate (api-key header → tenant_id).
  2. Forward the request to the upstream LLM (or call the mock LLM).
  3. If the response contains ``tool_calls`` whose function name matches a
     registered Plynf connector:
        a. Execute the tool (mock connector in MVP).
        b. Run the response through the policy engine.
        c. Emit a SavingsEvent.
        d. Append a ``role: tool`` message and re-call the LLM.
  4. Return the final assistant message in OpenAI format.

Endpoints:

  POST /v1/chat/completions   OpenAI-compatible
  GET  /healthz               liveness probe
  GET  /readyz                readiness probe (gates traffic during startup)
  GET  /metrics               Prometheus scrape (savings + per-tenant usage)
  GET  /v1/savings/summary    aggregate dashboard view
  GET  /v1/policies           list loaded connector policies
"""

from __future__ import annotations

import binascii
import json
import logging
import os
import struct
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .anthropic_adapter import anthropic_request_to_openai, openai_response_to_anthropic
from .bedrock_adapter import (
    bedrock_converse_request_to_openai,
    openai_response_to_bedrock_converse,
)
from .cache import TTLCache
from .cohere_adapter import cohere_chat_request_to_openai, openai_response_to_cohere_chat
from .connectors import (
    ConnectorRegistry,
    make_mock_registry,
)
from .context_budget import enforce_budget
from .error_envelopes import error_body
from .gateway_client import GatewayClient, make_gateway_registry
from .gemini_adapter import gemini_request_to_openai, openai_response_to_gemini
from .identity_client import IdentityClient, IdentityError
from .metrics import CONTENT_TYPE as METRICS_CONTENT_TYPE
from .metrics import render_metrics
from .mock_llm import (
    mock_completion,
    mock_embeddings,
    mock_model,
    mock_models,
    mock_text_completion,
)
from .policy_engine import ConnectorPolicy, PolicyError, ToolPolicy, apply, load_all_policies
from .policy_overrides import (
    PolicyOverrideStore,
    effective_policies_for_tenant,
    merge_override,
    policy_to_dict,
)
from .postgres_sink import PostgresSavingsSink
from .responses_adapter import openai_response_to_responses, responses_request_to_openai
from .rest_connector import build_rest_connector, specs_from_json
from .savings import SavingsEvent, SavingsSink, aggregate, make_event
from .settings import ProxySettings
from .tier_gate import TIERS, TierGate, upgrade_hint
from .tokens import count_json_tokens, count_messages_tokens
from .upstream_router import (
    HEADER_API_KEY,
    HEADER_BASE_URL,
    UpstreamRouter,
    parse_providers,
)

log = logging.getLogger("plinth.proxy")

# Loop-protection: max number of tool-call rounds per request.
MAX_TOOL_ROUNDS = 5


class AppState:
    settings: ProxySettings
    policies: dict[str, ConnectorPolicy]
    registry: ConnectorRegistry
    cache: TTLCache
    sink: SavingsSink | PostgresSavingsSink | None
    events: list[SavingsEvent]  # in-memory mirror for /v1/savings/summary
    api_keys: dict[str, str]    # key -> tenant_id
    api_key_tiers: dict[str, str]  # key -> tier (free/pro/enterprise)
    gate: TierGate
    identity: IdentityClient | None
    overrides: PolicyOverrideStore
    upstream_router: UpstreamRouter


def _instance_allows_custom_rest(settings: ProxySettings) -> bool:
    """True if any configured tier on this instance permits custom REST imports.

    Custom REST connectors are registered at the *instance* level (the
    self-hosted operator's own APIs), so the gate is the highest tier the
    instance is configured for: its ``demo_tier`` plus any per-key tiers.
    """
    tiers = {settings.demo_tier, *settings.parsed_api_key_tiers().values()}
    return any(
        (limits := TIERS.get(t)) is not None and limits.allow_custom_rest_connectors
        for t in tiers
    )


def _register_custom_rest_connectors(state: AppState, settings: ProxySettings) -> None:
    """Parse ``PLINTH_PROXY_REST_CONNECTORS`` and register each spec.

    Tier-gated: skipped (with a warning) when no configured tier permits custom
    REST connectors. Parse/registration errors are logged and skipped rather
    than crashing startup — a bad connector spec must not take the proxy down.
    """
    raw = (settings.rest_connectors or "").strip()
    if not raw:
        return
    if not _instance_allows_custom_rest(settings):
        log.warning(
            "PLINTH_PROXY_REST_CONNECTORS is set but no configured tier permits "
            "custom REST connectors (Pro+ only) — skipping."
        )
        return
    try:
        specs = specs_from_json(raw)
    except Exception as e:  # noqa: BLE001 - never let bad config crash startup
        log.error("failed to parse PLINTH_PROXY_REST_CONNECTORS: %s", e)
        return
    for spec in specs:
        try:
            tool_to_connector, handler = build_rest_connector(spec)
            state.registry.register(
                spec.name, handler, tools=list(tool_to_connector.keys())
            )
            log.info(
                "registered custom REST connector %r (%d tool(s))",
                spec.name,
                len(tool_to_connector),
            )
        except Exception as e:  # noqa: BLE001
            log.error("failed to register REST connector %r: %s", spec.name, e)


def _build_state(settings: ProxySettings, fixtures_dir: str | None = None) -> AppState:
    state = AppState()
    state.settings = settings
    state.policies = load_all_policies(settings.policies_path)
    # Connector wiring:
    #   - When PLINTH_PROXY_GATEWAY_URL is set AND demo_mode is False, route
    #     tool calls to the real plinth-gateway service.
    #   - Otherwise fall back to mock connectors (file-based fixtures).
    if settings.gateway_url and not settings.demo_mode:
        client = GatewayClient(
            settings.gateway_url,
            default_auth_header=(
                f"Bearer {settings.gateway_service_token}"
                if settings.gateway_service_token
                else None
            ),
        )
        state.registry = make_gateway_registry(client)
    else:
        # policies_path is .../services/proxy/src/plinth_proxy/policies (5 levels
        # deep under the repo root). Walk up five `parent`s to reach the repo
        # and find the demo fixtures bundled under examples/.
        if fixtures_dir is not None:
            fixtures = fixtures_dir
        else:
            env_override = os.environ.get("PLINTH_PROXY_FIXTURES_DIR")
            if env_override:
                fixtures = env_override
            else:
                policies_dir = settings.policies_path
                for _ in range(5):
                    policies_dir = policies_dir.parent
                fixtures = str(policies_dir / "examples" / "customer-support")
        state.registry = make_mock_registry(fixtures)
    _register_custom_rest_connectors(state, settings)
    state.cache = TTLCache()
    # Sink resolution: Postgres > JSONL > none.
    if settings.postgres_url:
        state.sink = PostgresSavingsSink(dsn=settings.postgres_url)
    elif settings.savings_log:
        state.sink = SavingsSink(path=Path(settings.savings_log))
    else:
        state.sink = None
    state.events = []
    state.api_keys = settings.parsed_api_keys()
    state.api_key_tiers = settings.parsed_api_key_tiers()
    state.gate = TierGate()
    state.identity = (
        IdentityClient(
            settings.identity_url,
            cache_ttl_s=settings.identity_cache_ttl_s,
        )
        if settings.identity_url
        else None
    )
    state.overrides = PolicyOverrideStore(
        Path(settings.policy_overrides_path) if settings.policy_overrides_path else None
    )
    # Multi-provider upstream routing. A bad PLINTH_PROXY_PROVIDERS blob must not
    # crash startup (mirrors custom REST connectors): log and degrade to
    # single-provider mode.
    try:
        providers = parse_providers((settings.providers or "").strip())
    except ValueError as e:
        log.error(
            "failed to parse PLINTH_PROXY_PROVIDERS: %s — using single-provider mode", e
        )
        providers = []
    state.upstream_router = UpstreamRouter(
        providers,
        default_base_url=settings.upstream_base_url,
        default_api_key=settings.upstream_api_key,
    )
    if providers:
        log.info(
            "multi-provider upstream routing enabled: %s",
            ", ".join(p.name for p in providers),
        )
    return state


@asynccontextmanager
async def _lifespan(app: FastAPI):
    settings = ProxySettings()
    app.state.plinth = _build_state(settings)
    log.info(
        "plinth-proxy %s ready: %d policies, mock=%s",
        __version__,
        len(app.state.plinth.policies),
        settings.demo_mode,
    )
    yield


class _RequestIDMiddleware:
    """Echo or mint an ``x-request-id`` header on every response.

    Vendor SDKs surface this header for support correlation — the OpenAI SDK
    exposes it as ``response.request_id`` and attaches it to raised errors, and
    the Anthropic SDK reads ``request-id`` — so a client that fronts Plynf would
    otherwise lose the request-id it had with the vendor. We honour an inbound
    id when present (length-capped to bound abuse) and mint a ``req_<hex>`` one
    otherwise, then set it on *every* response: success bodies, the dialect
    error envelopes, and SSE streams alike.

    Implemented as pure ASGI rather than ``BaseHTTPMiddleware`` so it stamps the
    header on the ``http.response.start`` message without buffering streaming
    bodies (``BaseHTTPMiddleware`` is the one that interferes with SSE).
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = (Headers(scope=scope).get("x-request-id") or "").strip()
        request_id = incoming[:200] if incoming else f"req_{uuid.uuid4().hex}"
        rid_bytes = request_id.encode("latin-1", "replace")

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() != b"x-request-id"
                ]
                headers.append((b"x-request-id", rid_bytes))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


def create_app(settings: ProxySettings | None = None) -> FastAPI:
    """Application factory. Allows tests to inject custom settings."""
    app = FastAPI(title="Plynf LLM-Proxy", version=__version__, lifespan=_lifespan)
    app.add_middleware(_RequestIDMiddleware)
    if settings is not None:
        app.state.plinth = _build_state(settings)

    @app.exception_handler(StarletteHTTPException)
    async def _dialect_error_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        """Reshape every HTTP error into the envelope its front door expects.

        FastAPI's default error body is ``{"detail": ...}``, which matches no
        vendor SDK — so a client that points its base URL at Plynf would see a
        foreign error shape on a 401/402/404 and its own error handling would
        break, defeating the "no code change" promise the success path keeps.
        :func:`error_body` keys off ``request.url.path`` to emit the OpenAI /
        Anthropic / Gemini / Cohere / Bedrock error shape instead. Registered on
        the Starlette base class so it also covers framework-raised errors (e.g.
        a 404 for an unmatched route), not just our own ``raise HTTPException``.
        Any ``exc.headers`` (e.g. a ``Retry-After`` on a 429) are preserved.
        """
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(request.url.path, exc.status_code, exc.detail),
            headers=getattr(exc, "headers", None),
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        st: AppState = app.state.plinth
        return {
            "status": "ok",
            "version": __version__,
            "policies_loaded": len(st.policies),
            "demo_mode": st.settings.demo_mode,
        }

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        """Readiness probe — distinct from ``/healthz`` liveness.

        ``/healthz`` answers "is the process alive?"; ``/readyz`` answers "has
        the app finished initializing and is it wired to serve traffic?".
        Kubernetes (and the Render/Fly/Railway deploy configs) gate traffic on
        this so requests aren't routed during the startup window. Returns 200
        once the app state is built, 503 before initialization completes, with
        a ``checks`` map describing each subsystem's wiring for observability.

        Subsystems are reported as *configured*, not live-pinged: a readiness
        probe must be cheap and flake-free, and Plynf uses identity/gateway
        lazily per request (a per-probe network round-trip would add latency
        and spurious flapping).
        """
        st: AppState | None = getattr(app.state, "plinth", None)
        if st is None:
            return JSONResponse(
                status_code=503,
                content={"status": "initializing", "ready": False},
            )

        checks = {
            "policies_loaded": len(st.policies),
            "connectors": "ok" if st.registry is not None else "missing",
            "identity": "configured" if st.identity is not None else "open-mode",
            "savings_sink": type(st.sink).__name__ if st.sink is not None else "none",
            "gateway": "configured"
            if (st.settings.gateway_url and not st.settings.demo_mode)
            else "mock",
        }
        ready = st.registry is not None
        return JSONResponse(
            status_code=200 if ready else 503,
            content={
                "status": "ready" if ready else "not_ready",
                "ready": ready,
                "version": __version__,
                "checks": checks,
            },
        )

    @app.get("/metrics")
    async def metrics() -> Response:
        """Prometheus scrape endpoint (text exposition format).

        Unauthenticated by convention — protect it at the network layer
        (Kubernetes ServiceMonitor / firewall / sidecar), the same way the
        kube-prometheus stack expects ``/metrics`` to be reachable. Exposes
        Plynf's core value metric (tokens saved) plus per-connector and
        per-tenant breakdowns for Grafana dashboards and alerting rules.
        """
        st: AppState = app.state.plinth
        body = render_metrics(
            st.events,
            version=__version__,
            tenant_usage=st.gate.all_usage(),
        )
        return Response(content=body, media_type=METRICS_CONTENT_TYPE)

    @app.get("/v1/policies")
    async def list_policies() -> dict[str, Any]:
        st: AppState = app.state.plinth
        return {
            "connectors": [
                {
                    "connector": p.connector,
                    "version": p.version,
                    "tools": list(p.tools.keys()),
                }
                for p in st.policies.values()
            ]
        }

    @app.get("/v1/connectors")
    async def list_connectors() -> dict[str, Any]:
        """List connectors this proxy can dispatch, with their tool names.

        Includes built-in connectors (mock fixtures or MCP-gateway-backed) and
        any custom REST connectors loaded from ``PLINTH_PROXY_REST_CONNECTORS``,
        so operators can confirm a custom import registered correctly.
        """
        st: AppState = app.state.plinth
        tools_by_connector = st.registry.list_tools()
        return {
            "connectors": [
                {"connector": name, "tools": tools, "tool_count": len(tools)}
                for name, tools in sorted(tools_by_connector.items())
            ]
        }

    @app.get("/v1/savings/summary")
    async def savings_summary() -> dict[str, Any]:
        st: AppState = app.state.plinth
        return aggregate(st.events)

    @app.get("/v1/savings/timeseries")
    async def savings_timeseries(bucket_s: int = 3600, limit: int = 168) -> dict[str, Any]:
        """Bucketed savings totals for the dashboard's line chart.

        ``bucket_s`` is the bucket width in seconds (default 1h). ``limit``
        caps how many trailing buckets to return (default 168 → 7 days of
        hourly buckets). The data comes from the in-memory event log; once
        the Postgres sink is the production source of truth this query
        moves into the DB layer.
        """
        st: AppState = app.state.plinth
        if bucket_s <= 0:
            raise HTTPException(status_code=400, detail="bucket_s must be > 0")

        buckets: dict[int, dict[str, float]] = {}
        for ev in st.events:
            key = int(ev.ts // bucket_s) * bucket_s
            b = buckets.setdefault(
                key,
                {
                    "saved_tokens": 0,
                    "shaped_tokens": 0,
                    "raw_tokens": 0,
                    "cost_saved_usd": 0.0,
                    "calls": 0,
                },
            )
            b["saved_tokens"] += ev.saved_tokens
            b["shaped_tokens"] += ev.shaped_response_tokens
            b["raw_tokens"] += ev.raw_response_tokens
            b["cost_saved_usd"] += ev.cost_saved_usd()
            b["calls"] += 1

        sorted_keys = sorted(buckets.keys())[-limit:]
        return {
            "bucket_s": bucket_s,
            "points": [
                {
                    "ts": k,
                    "saved_tokens": int(buckets[k]["saved_tokens"]),
                    "shaped_tokens": int(buckets[k]["shaped_tokens"]),
                    "raw_tokens": int(buckets[k]["raw_tokens"]),
                    "cost_saved_usd": round(buckets[k]["cost_saved_usd"], 6),
                    "calls": int(buckets[k]["calls"]),
                }
                for k in sorted_keys
            ],
        }

    @app.get("/v1/policies/effective")
    async def effective_policies(request: Request) -> dict[str, Any]:
        """Per-tenant effective policies (system default + override) for the editor."""
        st: AppState = app.state.plinth
        tenant_id, _tier = await _authenticate(request, st)
        return {
            "tenant_id": tenant_id,
            "tools": effective_policies_for_tenant(
                st.policies, st.overrides, tenant_id
            ),
        }

    @app.put("/v1/policies/{connector}/{tool}/override")
    async def set_override(connector: str, tool: str, request: Request) -> dict[str, Any]:
        """Replace this tenant's override for one tool. Body is a partial policy."""
        st: AppState = app.state.plinth
        tenant_id, tier = await _authenticate(request, st)
        body = await request.json()
        if "redact_pii" in body and tier == "free":
            raise HTTPException(
                status_code=402,
                detail={
                    "error": "tier_limit_exceeded",
                    "reason": "feature_requires_pro",
                    "tier": tier,
                    "upgrade_hint": upgrade_hint("free"),
                },
            )
        cp = st.policies.get(connector)
        if cp is None or tool not in cp.tools:
            raise HTTPException(
                status_code=404, detail=f"unknown tool: {connector}/{tool}"
            )
        st.overrides.set(tenant_id, connector, tool, body)
        effective = _policy_for(st, connector, tool, tenant_id)
        return {"ok": True, "policy": policy_to_dict(connector, effective)}

    @app.delete("/v1/policies/{connector}/{tool}/override")
    async def clear_override(connector: str, tool: str, request: Request) -> dict[str, Any]:
        """Drop this tenant's override; revert to the shipped default."""
        st: AppState = app.state.plinth
        tenant_id, _tier = await _authenticate(request, st)
        st.overrides.clear(tenant_id, connector, tool)
        return {"ok": True}

    @app.post("/v1/tools/{tool_name}/invoke")
    async def webhook_invoke(tool_name: str, request: Request) -> JSONResponse:
        """Generic tool-invocation webhook.

        One HTTP call to Plynf executes the named tool *and* shapes the
        response. Used by AWS Bedrock action groups, custom enterprise
        agents, Slack AI integrations, or anything else that prefers a
        flat REST contract to the chat-completions round-trip.

        Body::

            {"arguments": {...}, "agent_id": "...", "workflow_id": "..."}

        Response::

            {"tool": "get_order", "result": <shaped>, "savings": {...}}
        """
        st: AppState = app.state.plinth
        tenant_id, tier = await _authenticate(request, st)
        _enforce_tier(st, tenant_id, tier)

        if st.registry.resolve(tool_name) is None:
            raise HTTPException(status_code=404, detail=f"unknown tool: {tool_name}")

        body = await request.json() if await _has_body(request) else {}
        args = body.get("arguments") or {}
        agent_id = body.get("agent_id")
        workflow_id = body.get("workflow_id")

        connector_name = st.registry.resolve(tool_name) or "unknown"
        policy = _policy_for(st, connector_name, tool_name, tenant_id)

        # Cache lookup, same as the chat-completions path.
        cache_key = (
            f"{tenant_id}:{connector_name}:{tool_name}:"
            f"{json.dumps(args, sort_keys=True)}"
        )
        hit, cached = st.cache.get(cache_key)
        if hit:
            raw = cached["raw"]
            shaped = cached["shaped"]
            cache_hit = True
        else:
            try:
                _conn, raw = await st.registry.execute(tool_name, args)
            except Exception as e:
                raise HTTPException(
                    status_code=502, detail=f"tool execution failed: {e!s}"
                ) from e
            try:
                from .policy_engine import apply

                shaped = apply(raw, policy)
            except PolicyError as pe:
                raise HTTPException(status_code=403, detail=str(pe)) from pe
            cache_hit = False
            if policy.cache_ttl:
                st.cache.set(cache_key, {"raw": raw, "shaped": shaped}, policy.cache_ttl)

        raw_tokens = count_json_tokens(raw)
        shaped_tokens = count_json_tokens(shaped)
        event = make_event(
            tenant_id=tenant_id,
            agent_id=agent_id,
            connector=connector_name,
            tool=tool_name,
            model=body.get("model") or st.settings.default_model,
            raw_response_tokens=raw_tokens,
            shaped_response_tokens=shaped_tokens,
            cache_hit=cache_hit,
            request_args=args,
            workflow_id=workflow_id,
        )
        st.events.append(event)
        if st.sink is not None:
            st.sink.emit(event)
        st.gate.record_tokens(tenant_id, shaped_tokens)

        return JSONResponse(
            {
                "tool": tool_name,
                "connector": connector_name,
                "result": shaped,
                "cache_hit": cache_hit,
                "savings": {
                    "raw_response_tokens": raw_tokens,
                    "shaped_response_tokens": shaped_tokens,
                    "saved_tokens": event.saved_tokens,
                    "savings_pct": round(event.savings_pct, 4),
                },
            }
        )

    @app.get("/v1/tier")
    async def tier_info(request: Request) -> dict[str, Any]:
        """Show the caller's current tier + month-to-date usage."""
        st: AppState = app.state.plinth
        tenant_id, tier = await _authenticate(request, st)
        return {
            "tenant_id": tenant_id,
            "tier": tier,
            "tokens_used_this_month": st.gate.usage(tenant_id),
        }

    @app.post("/v1/shape")
    async def shape(request: Request) -> JSONResponse:
        """Client-side shaping endpoint for the SDK.

        Used by ``plinth.proxy_client.wrap_tool`` — the agent ran the tool
        itself and now wants the response shaped before injecting it into
        the LLM context. Body shape::

            {"tool": "get_order", "raw_response": {...}, "tenant_id": "..."}

        Response::

            {"shaped": {...}, "raw_response_tokens": N, "shaped_response_tokens": M,
             "saved_tokens": N-M, "savings_pct": ...}
        """
        st: AppState = app.state.plinth
        body = await request.json()
        tenant_id, tier = await _authenticate(request, st)
        _enforce_tier(st, tenant_id, tier)
        tool_name = body.get("tool")
        raw = body.get("raw_response")
        if not tool_name:
            raise HTTPException(status_code=400, detail="'tool' is required")

        if st.registry.resolve(tool_name) is None:
            # Unknown tool → pass through. Caller can still use Plynf for
            # the tools that do have policies.
            return JSONResponse({"shaped": raw, "shaped_by_plynf": False})

        connector_name = st.registry.resolve(tool_name) or "unknown"
        policy = _policy_for(st, connector_name, tool_name, tenant_id)

        try:
            from .policy_engine import apply

            shaped = apply(raw, policy)
        except PolicyError as pe:
            raise HTTPException(status_code=403, detail=str(pe)) from pe

        raw_tokens = count_json_tokens(raw)
        shaped_tokens = count_json_tokens(shaped)

        event = make_event(
            tenant_id=tenant_id,
            agent_id=None,
            connector=connector_name,
            tool=tool_name,
            model=body.get("model") or st.settings.default_model,
            raw_response_tokens=raw_tokens,
            shaped_response_tokens=shaped_tokens,
            cache_hit=False,
            request_args=body.get("request_args") or {},
        )
        st.events.append(event)
        if st.sink is not None:
            st.sink.emit(event)

        # Count shaped tokens against the tenant's monthly budget. Raw tokens
        # were never going to be billed by Plynf — only the value-delivered
        # (shaped) portion counts toward your tier.
        st.gate.record_tokens(tenant_id, shaped_tokens)

        return JSONResponse(
            {
                "shaped": shaped,
                "shaped_by_plynf": True,
                "raw_response_tokens": raw_tokens,
                "shaped_response_tokens": shaped_tokens,
                "saved_tokens": raw_tokens - shaped_tokens,
                "savings_pct": round(
                    (raw_tokens - shaped_tokens) / raw_tokens, 4
                ) if raw_tokens else 0.0,
            }
        )

    @app.post("/v1beta/models/{model}:generateContent")
    async def gemini_generate_content(model: str, request: Request) -> JSONResponse:
        """Google Gemini (public API) ``generateContent`` endpoint.

        Translates the Gemini request to OpenAI, runs the same Plynf pipeline
        (auth → tier-gate → tool-call interception → shaping → savings),
        then translates back to a Gemini ``candidates`` envelope.

        Streaming is served by the sibling ``:streamGenerateContent`` route
        (``?alt=sse`` for SSE framing, else a JSON array of responses).
        """
        st: AppState = app.state.plinth
        body, headers = await _run_gemini_dialect(st, request, model)
        return JSONResponse(body, headers=headers)

    @app.post("/v1beta/models/{model}:streamGenerateContent")
    async def gemini_stream_generate_content(model: str, request: Request) -> Response:
        """Google Gemini (public API) ``streamGenerateContent`` — streaming.

        Runs the identical pipeline as ``:generateContent`` (tool-call
        interception always completes synchronously), then re-emits the final
        candidate as a stream: ``?alt=sse`` yields ``data:``-framed SSE chunks
        (what the google-genai SDK requests), otherwise a JSON array of
        responses. A Gemini client streaming through Plynf needs no code change.
        """
        st: AppState = app.state.plinth
        body, headers = await _run_gemini_dialect(st, request, model)
        return _gemini_stream_or_array(request, body, headers)

    @app.post(
        "/v1/projects/{project}/locations/{location}"
        "/publishers/google/models/{model}:generateContent"
    )
    async def vertex_generate_content(
        project: str, location: str, model: str, request: Request
    ) -> JSONResponse:
        """Google Vertex AI ``generateContent`` endpoint.

        Vertex serves the *same* request/response body as the public Gemini
        API but at a project/location-scoped path, so this reuses the Gemini
        translators verbatim — a Vertex client only needs its base URL
        pointed at Plynf. ``project`` / ``location`` are captured for routing
        fidelity (a Vertex SDK builds this exact path) but the MVP does not
        per-project-route upstreams.
        """
        st: AppState = app.state.plinth
        body, headers = await _run_gemini_dialect(st, request, model)
        return JSONResponse(body, headers=headers)

    @app.post(
        "/v1/projects/{project}/locations/{location}"
        "/publishers/google/models/{model}:streamGenerateContent"
    )
    async def vertex_stream_generate_content(
        project: str, location: str, model: str, request: Request
    ) -> Response:
        """Vertex AI ``streamGenerateContent`` — the streaming variant.

        Same native-path reuse as the unary Vertex route: it shares the Gemini
        translators and the streaming synthesis, differing only in the
        project/location-scoped URL. ``?alt=sse`` selects SSE framing, otherwise
        a JSON array of responses is returned.
        """
        st: AppState = app.state.plinth
        body, headers = await _run_gemini_dialect(st, request, model)
        return _gemini_stream_or_array(request, body, headers)

    @app.post("/model/{model_id:path}/converse")
    async def bedrock_converse(model_id: str, request: Request) -> JSONResponse:
        """AWS Bedrock runtime ``Converse`` endpoint.

        Translates the Bedrock Converse request to OpenAI, runs the same
        Plynf pipeline (auth → tier-gate → tool-call interception →
        shaping → savings), then translates back to a Converse
        ``{output, stopReason, usage}`` envelope. One adapter covers every
        Bedrock-hosted model (Claude, Llama, Titan, Mistral, Command …)
        because they all share the Converse message shape.

        ``{model_id:path}`` so provider-qualified ARNs / IDs that contain
        slashes (e.g. ``anthropic.claude-3-5-sonnet-20241022-v2:0``) match.

        The streaming sibling is ``ConverseStream`` (``/converse-stream``)
        below.
        """
        st: AppState = app.state.plinth
        bedrock_body = await request.json()
        bedrock_final, headers = await _run_bedrock_converse(st, request, bedrock_body, model_id)
        return JSONResponse(bedrock_final, headers=headers)

    @app.post("/model/{model_id:path}/converse-stream")
    async def bedrock_converse_stream(model_id: str, request: Request) -> Response:
        """AWS Bedrock runtime ``ConverseStream`` endpoint.

        The streaming sibling of ``Converse``. Bedrock does not stream SSE — it
        frames the response as the AWS *event stream* binary protocol
        (``vnd.amazon.eventstream``): each event is a length-prefixed message
        with a CRC32-checksummed prelude, typed headers (``:event-type`` ∈
        ``messageStart`` / ``contentBlockDelta`` / ``contentBlockStop`` /
        ``messageStop`` / ``metadata`` …), a JSON payload, and a trailing
        message CRC32. boto3 / the AWS SDKs decode exactly this. Plynf runs the
        request unary (tool-call interception must finish first) and replays the
        shaped final body as that binary event sequence, so a Bedrock client
        streaming through Plynf needs no code change and the per-call savings
        headers ride along.
        """
        st: AppState = app.state.plinth
        bedrock_body = await request.json()
        bedrock_final, headers = await _run_bedrock_converse(st, request, bedrock_body, model_id)
        return StreamingResponse(
            _synthesize_bedrock_converse_stream(bedrock_final),
            media_type="application/vnd.amazon.eventstream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", **headers},
        )

    @app.post("/v2/chat")
    async def cohere_chat(request: Request) -> Response:
        """Cohere v2 ``/v2/chat`` endpoint.

        Translates the Cohere v2 chat request to OpenAI, runs the same Plynf
        pipeline (auth → tier-gate → tool-call interception → shaping →
        savings), then translates back to a Cohere ``{message, finish_reason,
        usage}`` envelope. A Cohere SDK client only needs its base URL pointed
        at Plynf — no code change — to get response-shaping savings.

        Streaming (``stream: true``) is served as Cohere v2's typed-event SSE
        (``message-start`` → ``content-delta``\\* → ``message-end``). Plynf
        computes the result unary (tool-call interception must finish first)
        and replays the shaped final message as that event sequence, so a
        Cohere client streaming through Plynf needs no code change and the
        per-call savings headers ride along.
        """
        st: AppState = app.state.plinth
        cohere_body = await request.json()
        wants_stream = bool(cohere_body.get("stream"))
        tenant_id, tier = await _authenticate(request, st)
        _enforce_tier(st, tenant_id, tier)

        openai_body = cohere_chat_request_to_openai(cohere_body)
        openai_body["stream"] = False

        before = len(st.events)
        openai_final = await _handle_chat(st, openai_body, tenant_id)
        new_shaped = sum(
            ev.shaped_response_tokens for ev in st.events[before:]
        )
        if new_shaped:
            st.gate.record_tokens(tenant_id, new_shaped)

        cohere_final = openai_response_to_cohere_chat(openai_final)
        headers = _savings_headers(st, before)
        if not wants_stream:
            return JSONResponse(cohere_final, headers=headers)
        return StreamingResponse(
            _synthesize_cohere_sse(cohere_final),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", **headers},
        )

    @app.post("/v1/messages")
    async def anthropic_messages(request: Request) -> JSONResponse:
        """Anthropic-compatible /v1/messages endpoint.

        Translates the Anthropic-shaped request to OpenAI, runs the same
        Plynf pipeline (auth → tier-gate → tool-call interception →
        shaping → savings), then translates the OpenAI result back to
        Anthropic's message shape so SDK clients see the wire format they
        expect.

        Streaming is not supported on this endpoint in the MVP; Anthropic
        SSE has a different chunk schema than OpenAI's. Pass-through when
        we ship streaming-anthropic in a later iteration.
        """
        st: AppState = app.state.plinth
        anth_body = await request.json()
        wants_stream = bool(anth_body.get("stream"))
        anth_final, headers = await _run_anthropic_dialect(st, request, anth_body)
        if not wants_stream:
            return JSONResponse(anth_final, headers=headers)
        return StreamingResponse(
            _synthesize_anthropic_sse(anth_final),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", **headers},
        )

    @app.post(
        "/v1/projects/{project}/locations/{location}"
        "/publishers/anthropic/models/{model}:rawPredict"
    )
    async def vertex_anthropic_raw_predict(
        project: str, location: str, model: str, request: Request
    ) -> JSONResponse:
        """Anthropic Claude on Vertex AI (``:rawPredict``) endpoint.

        Vertex serves Claude through the *native Anthropic Messages* body at a
        project/location-scoped ``publishers/anthropic`` path, carrying the
        model in the URL and an ``anthropic_version`` field in the body instead
        of a ``model`` field. So this reuses the Anthropic translators verbatim
        — the same native-path-reuse trick as the Gemini → Vertex routes — and
        only injects the path model when the body omits it. A Vertex-Claude
        client fronts Plynf by pointing its base URL here, no code change.

        ``project`` / ``location`` are captured for routing fidelity (a Vertex
        SDK builds this exact path) but the MVP does not per-project-route
        upstreams. The streaming sibling is ``:streamRawPredict`` below.
        """
        st: AppState = app.state.plinth
        anth_body = await request.json()
        anth_body.setdefault("model", model)
        body, headers = await _run_anthropic_dialect(st, request, anth_body)
        return JSONResponse(body, headers=headers)

    @app.post(
        "/v1/projects/{project}/locations/{location}"
        "/publishers/anthropic/models/{model}:streamRawPredict"
    )
    async def vertex_anthropic_stream_raw_predict(
        project: str, location: str, model: str, request: Request
    ) -> Response:
        """Anthropic Claude on Vertex AI (``:streamRawPredict``) endpoint.

        The streaming sibling of ``:rawPredict``. "Raw" is load-bearing: Vertex
        passes the *native Anthropic SSE* stream through verbatim (``event:
        message_start`` → ``content_block_delta`` → ``message_stop``), the same
        taxonomy ``POST /v1/messages`` streams — not an OpenAI chunk stream. So
        this reuses the Anthropic translators and ``_synthesize_anthropic_sse``
        exactly like the unary route reuses ``:rawPredict``. The method suffix
        is the streaming contract (a ``stream`` body flag is not required), so
        this route always streams. Plynf runs the request unary (tool-call
        interception must finish first) and replays the shaped final body as
        that event sequence, carrying the per-call savings headers.
        """
        st: AppState = app.state.plinth
        anth_body = await request.json()
        anth_body.setdefault("model", model)
        body, headers = await _run_anthropic_dialect(st, request, anth_body)
        return StreamingResponse(
            _synthesize_anthropic_sse(body),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", **headers},
        )

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        st: AppState = app.state.plinth
        body = await request.json()
        return await _run_openai_chat(st, request, body)

    @app.post("/openai/deployments/{deployment}/chat/completions")
    async def azure_chat_completions(deployment: str, request: Request):
        """Azure OpenAI-compatible chat-completions endpoint.

        The Azure OpenAI SDK posts to ``/openai/deployments/{deployment}/
        chat/completions?api-version=...`` with an ``api-key`` header and
        carries the model as the *deployment* path segment rather than a
        ``model`` body field. The request/response bodies are otherwise
        identical to OpenAI, so we only fill in ``model`` from the deployment
        name (when the body omits it) and run the standard pipeline — an Azure
        shop fronts Plynf by changing only its base URL.
        """
        st: AppState = app.state.plinth
        body = await request.json()
        body.setdefault("model", deployment)
        return await _run_openai_chat(st, request, body)

    @app.post("/v1/responses")
    async def responses(request: Request) -> Response:
        """OpenAI *Responses* API endpoint (the successor to chat-completions).

        Translates the Responses request (``input`` items + ``instructions`` +
        flat function ``tools``) to OpenAI chat shape, runs the same Plynf
        pipeline (auth → tier-gate → tool-call interception → shaping →
        savings), then translates back to a Responses ``{output, output_text,
        status, usage}`` envelope. A client that posts to ``/v1/responses``
        only needs its base URL pointed at Plynf — no code change.

        When the body sets ``stream: true`` the result is re-emitted as the
        Responses typed-event SSE taxonomy (``response.created`` → per-item
        ``output_text.delta`` / ``function_call_arguments.delta`` →
        ``response.completed``); interception still runs unary first, so the
        stream replays the already-shaped final body.
        """
        st: AppState = app.state.plinth
        resp_body = await request.json()
        wants_stream = bool(resp_body.get("stream"))
        tenant_id, tier = await _authenticate(request, st)
        _enforce_tier(st, tenant_id, tier)

        openai_body = responses_request_to_openai(resp_body)
        openai_body["stream"] = False

        before = len(st.events)
        openai_final = await _handle_chat(st, openai_body, tenant_id)
        new_shaped = sum(ev.shaped_response_tokens for ev in st.events[before:])
        if new_shaped:
            st.gate.record_tokens(tenant_id, new_shaped)

        final_resp = openai_response_to_responses(openai_final)
        headers = _savings_headers(st, before)
        if not wants_stream:
            return JSONResponse(final_resp, headers=headers)
        return StreamingResponse(
            _synthesize_responses_sse(final_resp),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", **headers},
        )

    # -- OpenAI drop-in completeness -------------------------------------
    # Clients that set OPENAI_BASE_URL=<plynf> probe these on startup and use
    # them for non-chat work. Plynf doesn't shape either (no tool response to
    # trim), so they forward verbatim to the configured upstream and fall back
    # to deterministic mocks in demo / offline mode — keeping Plynf a true
    # drop-in rather than a chat-only proxy.

    @app.get("/v1/models")
    async def list_models(request: Request) -> JSONResponse:
        st: AppState = app.state.plinth
        # Listing is metadata: authenticate to resolve the tenant, but it is
        # neither tier-gated nor charged.
        await _authenticate(request, st)
        if st.settings.upstream_base_url and not st.settings.demo_mode:
            return JSONResponse(await _forward_upstream(st, "GET", "/v1/models"))
        return JSONResponse(mock_models())

    @app.get("/v1/models/{model}")
    async def retrieve_model(model: str, request: Request) -> JSONResponse:
        st: AppState = app.state.plinth
        await _authenticate(request, st)
        if st.settings.upstream_base_url and not st.settings.demo_mode:
            return JSONResponse(await _forward_upstream(st, "GET", f"/v1/models/{model}"))
        return JSONResponse(mock_model(model))

    @app.post("/v1/embeddings")
    async def embeddings(request: Request) -> JSONResponse:
        st: AppState = app.state.plinth
        body = await request.json()
        tenant_id, tier = await _authenticate(request, st)
        # Enforce the gate (an over-budget tenant is blocked) but don't record
        # tokens — embeddings aren't shaped, so they don't count as savings.
        _enforce_tier(st, tenant_id, tier)
        if st.settings.upstream_base_url and not st.settings.demo_mode:
            return JSONResponse(
                await _forward_upstream(st, "POST", "/v1/embeddings", json_body=body)
            )
        return JSONResponse(mock_embeddings(body))

    @app.post("/v1/completions")
    async def completions(request: Request) -> JSONResponse:
        """OpenAI legacy *text* completions (the pre-chat ``/v1/completions``).

        Some clients still target the legacy completions endpoint — LangChain's
        ``OpenAI`` LLM class, llama-index's ``OpenAI`` completion mode, and
        older scripts. It predates tool-calling, so Plynf shapes nothing here;
        like ``/v1/embeddings`` it gates the tenant (an over-budget caller is
        blocked) but doesn't charge (no tool response → no savings), forwarding
        verbatim to the configured upstream and falling back to a deterministic
        mock in demo / offline mode so a client pointed entirely at Plynf keeps
        working rather than getting a 404.
        """
        st: AppState = app.state.plinth
        body = await request.json()
        tenant_id, tier = await _authenticate(request, st)
        _enforce_tier(st, tenant_id, tier)
        if st.settings.upstream_base_url and not st.settings.demo_mode:
            return JSONResponse(
                await _forward_upstream(st, "POST", "/v1/completions", json_body=body)
            )
        return JSONResponse(mock_text_completion(body))

    return app


# ---------------------------------------------------------------------------
# Authentication (minimal)
# ---------------------------------------------------------------------------


def _extract_token(request: Request) -> str | None:
    """Pull the caller's token from the Authorization or ``api-key`` header.

    Accepts ``Authorization: Bearer <token>`` (OpenAI / Anthropic / Cohere /
    most SDKs) and the ``api-key: <token>`` header the Azure OpenAI SDK sends.
    Returns ``None`` when neither is present.
    """
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    api_key = request.headers.get("api-key")
    return api_key.strip() if api_key else None


async def _authenticate(request: Request, st: AppState) -> tuple[str, str]:
    """Resolve the caller's token → ``(tenant_id, tier)``.

    The token is read from ``Authorization: Bearer <token>`` or the Azure-style
    ``api-key`` header (see :func:`_extract_token`). Resolution order:

      1. **Static api_keys map** — fast path for self-hosted / demo setups.
         Configured via ``PLINTH_PROXY_API_KEYS``.
      2. **Identity service JWT verify** — if ``PLINTH_PROXY_IDENTITY_URL`` is
         set, the token is forwarded to ``/v1/tokens/verify`` and the
         tenant_id + tier are read off the signed claims.
      3. **Open mode** — no api_keys, no identity URL → caller is labelled
         ``demo`` at the configured ``demo_tier`` (default ``enterprise``).

    Returns ``(tenant_id, tier)`` or raises ``401``.
    """
    token = _extract_token(request)

    # 1. Static map fast-path.
    if st.api_keys:
        if token is None:
            raise HTTPException(status_code=401, detail="missing api key")
        tenant = st.api_keys.get(token)
        if tenant is not None:
            return tenant, st.api_key_tiers.get(token, "free")
        # Static map present but key didn't match — fall through to identity
        # if configured, otherwise 401.
        if st.identity is None:
            raise HTTPException(status_code=401, detail="unknown api key")

    # 2. Identity-service verify.
    if st.identity is not None:
        if token is None:
            raise HTTPException(status_code=401, detail="missing bearer token")
        try:
            claims = await st.identity.verify(token)
        except IdentityError as ie:
            raise HTTPException(
                status_code=401 if ie.status in (401, 403) else 503,
                detail=str(ie),
            ) from ie
        return claims.tenant_id, claims.tier

    # 3. Open mode (no auth configured).
    return "demo", st.settings.demo_tier


def _enforce_tier(st: AppState, tenant_id: str, tier: str) -> None:
    """Raise 402 if the tenant's tier doesn't cover this call."""
    allowed, reason = st.gate.check(tenant_id, tier)  # type: ignore[arg-type]
    if not allowed:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "tier_limit_exceeded",
                "reason": reason,
                "tier": tier,
                "upgrade_hint": upgrade_hint(tier),  # type: ignore[arg-type]
            },
        )


# ---------------------------------------------------------------------------
# Per-call savings headers (attached by every chat / native-dialect front door)
# ---------------------------------------------------------------------------


def _savings_headers(st: AppState, before: int) -> dict[str, str]:
    """Summarize the savings events emitted *during this request* as headers.

    Plynf's value is token reduction, so every chat response advertises it
    inline — no need to poll ``/v1/savings/summary``. ``before`` is
    ``len(st.events)`` captured before the request ran; the slice
    ``st.events[before:]`` is exactly the interceptions this call produced
    (zero for a plain completion with no tool calls). Header values are plain
    ASCII integers / a 4-dp ratio so any HTTP client can read them.
    """
    window = st.events[before:]
    raw = sum(ev.raw_response_tokens for ev in window)
    shaped = sum(ev.shaped_response_tokens for ev in window)
    saved = sum(ev.saved_tokens for ev in window)
    pct = (saved / raw) if raw else 0.0
    return {
        "X-Plynf-Tool-Calls": str(len(window)),
        "X-Plynf-Raw-Tokens": str(raw),
        "X-Plynf-Shaped-Tokens": str(shaped),
        "X-Plynf-Saved-Tokens": str(saved),
        "X-Plynf-Savings-Pct": f"{pct:.4f}",
    }


# ---------------------------------------------------------------------------
# OpenAI chat dialect (shared by native /v1 and Azure deployment paths)
# ---------------------------------------------------------------------------


async def _run_openai_chat(st: AppState, request: Request, body: dict[str, Any]):
    """Authenticate, gate, shape, charge, and return an OpenAI chat response.

    Shared by the native OpenAI ``/v1/chat/completions`` route and the
    Azure-style ``/openai/deployments/{deployment}/chat/completions`` route —
    both speak the identical OpenAI body, differing only in URL and auth
    header. Streaming clients get synthesized SSE; others get JSON.
    """
    tenant_id, tier = await _authenticate(request, st)
    _enforce_tier(st, tenant_id, tier)
    wants_stream = bool(body.get("stream"))
    # Per-request upstream override (escape hatch for ad-hoc base URLs); the
    # provider/model prefix is handled inside the router from the model string.
    header_base_url = request.headers.get(HEADER_BASE_URL)
    header_api_key = request.headers.get(HEADER_API_KEY)
    # Tool-call interception requires holding the response until we've shaped
    # and re-called, so we always run the full flow first, then synthesize SSE
    # chunks for streaming clients — same OpenAI contract, same content.
    before_count = len(st.events)
    final = await _handle_chat(
        st,
        body,
        tenant_id,
        header_base_url=header_base_url,
        header_api_key=header_api_key,
    )
    new_shaped_tokens = sum(ev.shaped_response_tokens for ev in st.events[before_count:])
    if new_shaped_tokens:
        st.gate.record_tokens(tenant_id, new_shaped_tokens)

    headers = _savings_headers(st, before_count)
    if not wants_stream:
        return JSONResponse(final, headers=headers)
    return StreamingResponse(
        _synthesize_sse(final),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", **headers},
    )


# ---------------------------------------------------------------------------
# Gemini dialect (shared by the public Gemini API and Vertex AI paths)
# ---------------------------------------------------------------------------


async def _run_gemini_dialect(
    st: AppState, request: Request, model: str
) -> tuple[dict[str, Any], dict[str, str]]:
    """Run a Gemini-shaped request through the pipeline; return (body, headers).

    Shared by ``/v1beta/models/{model}:generateContent`` (public Gemini) and
    the Vertex AI project-scoped path — both speak the identical body shape,
    so only the URL differs. Mirrors the auth → tier-gate → shape → charge
    flow used by every other native-dialect front door. The second tuple
    element is the per-call ``X-Plynf-*`` savings headers for the route to
    attach to its response.
    """
    gem_body = await request.json()
    tenant_id, tier = await _authenticate(request, st)
    _enforce_tier(st, tenant_id, tier)

    openai_body = gemini_request_to_openai(gem_body, model=model)
    openai_body["stream"] = False

    before = len(st.events)
    openai_final = await _handle_chat(st, openai_body, tenant_id)
    new_shaped = sum(ev.shaped_response_tokens for ev in st.events[before:])
    if new_shaped:
        st.gate.record_tokens(tenant_id, new_shaped)

    return openai_response_to_gemini(openai_final), _savings_headers(st, before)


def _gemini_stream_or_array(
    request: Request, body: dict[str, Any], headers: dict[str, str]
) -> Response:
    """Frame a completed Gemini candidate for the ``:streamGenerateContent`` route.

    Gemini's streaming method emits SSE (``data: {GenerateContentResponse}``)
    when the client passes ``?alt=sse`` — the framing the google-genai SDK uses
    — and a JSON array of responses otherwise. Plynf always computes the result
    unary (tool-call interception must complete first), so both forms are
    synthesized from the same final ``body``, and the per-call ``X-Plynf-*``
    savings headers ride along on either.
    """
    if request.query_params.get("alt") == "sse":
        return StreamingResponse(
            _synthesize_gemini_sse(body),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", **headers},
        )
    return JSONResponse([body], headers=headers)


# ---------------------------------------------------------------------------
# Anthropic dialect (shared by the public /v1/messages and Vertex-Claude paths)
# ---------------------------------------------------------------------------


async def _run_anthropic_dialect(
    st: AppState, request: Request, anth_body: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, str]]:
    """Run an Anthropic-shaped request through the pipeline; return (body, headers).

    Shared by ``POST /v1/messages`` (public Anthropic) and the Vertex AI
    ``publishers/anthropic/...:rawPredict`` path — both speak the identical
    Messages body, so only the URL (and where the model comes from) differs.
    Returns the translated Anthropic response dict plus the per-call
    ``X-Plynf-*`` savings headers; the caller decides whether to wrap the body
    in synthesized SSE (``/v1/messages`` streaming) or return it unary
    (Vertex). Mirrors the auth → tier-gate → shape → charge flow every other
    native-dialect front door uses.
    """
    tenant_id, tier = await _authenticate(request, st)
    _enforce_tier(st, tenant_id, tier)

    openai_body = anthropic_request_to_openai(anth_body)
    # The OpenAI pipeline never needs to stream — tool-call interception always
    # runs synchronously and SSE (when wanted) is re-emitted from the final shape.
    openai_body["stream"] = False

    before = len(st.events)
    openai_final = await _handle_chat(st, openai_body, tenant_id)
    new_shaped = sum(ev.shaped_response_tokens for ev in st.events[before:])
    if new_shaped:
        st.gate.record_tokens(tenant_id, new_shaped)

    return openai_response_to_anthropic(openai_final), _savings_headers(st, before)


async def _run_bedrock_converse(
    st: AppState, request: Request, bedrock_body: dict[str, Any], model_id: str
) -> tuple[dict[str, Any], dict[str, str]]:
    """Run a Bedrock Converse request through the pipeline; return (body, headers).

    Shared by ``POST /model/{id}/converse`` (unary) and ``/converse-stream``
    (AWS event-stream) — both speak the identical Converse body, so only the
    response framing differs. Returns the translated Converse response dict
    plus the per-call ``X-Plynf-*`` savings headers; the caller decides whether
    to return it as JSON or wrap it in the synthesized binary event stream.
    Mirrors the auth → tier-gate → shape → charge flow every other native
    front door uses.
    """
    tenant_id, tier = await _authenticate(request, st)
    _enforce_tier(st, tenant_id, tier)

    openai_body = bedrock_converse_request_to_openai(bedrock_body, model=model_id)
    openai_body["stream"] = False

    before = len(st.events)
    openai_final = await _handle_chat(st, openai_body, tenant_id)
    new_shaped = sum(ev.shaped_response_tokens for ev in st.events[before:])
    if new_shaped:
        st.gate.record_tokens(tenant_id, new_shaped)

    return openai_response_to_bedrock_converse(openai_final), _savings_headers(st, before)


# ---------------------------------------------------------------------------
# Chat-completion handler
# ---------------------------------------------------------------------------


async def _handle_chat(
    st: AppState,
    body: dict[str, Any],
    tenant_id: str,
    *,
    header_base_url: str | None = None,
    header_api_key: str | None = None,
) -> dict[str, Any]:
    messages: list[dict[str, Any]] = list(body.get("messages") or [])
    tools: list[dict[str, Any]] | None = body.get("tools")
    model: str = body.get("model") or st.settings.default_model

    for _round in range(MAX_TOOL_ROUNDS):
        # Context-budget rotation runs before every LLM call, not just at the
        # end — keeps the input shape stable across rounds.
        if st.settings.context_budget_input_tokens > 0:
            messages, dropped = enforce_budget(
                messages,
                max_input_tokens=st.settings.context_budget_input_tokens,
                keep_recent_tool_messages=st.settings.context_budget_keep_recent_tool_messages,
            )
            if dropped:
                log.info(
                    "context-budget rotated tool messages: %d tokens dropped",
                    dropped,
                )

        response = await _call_upstream(
            st,
            messages,
            tools,
            body,
            model,
            header_base_url=header_base_url,
            header_api_key=header_api_key,
        )

        choice = (response.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        tool_calls = message.get("tool_calls") or []

        if not tool_calls:
            # Final answer — return as-is.
            return response

        # Append the assistant message so the next round has full history.
        messages.append(message)

        # Cross-call merging: when the LLM emits multiple identical
        # tool_calls in one round (same name + same arguments), execute the
        # tool ONCE and share the shaped result across every tool_call_id.
        # The first occurrence emits a normal savings event; duplicates emit
        # a "merged" savings event (cache_hit=True) so the dashboard counts
        # the dedup benefit.
        in_round_cache: dict[str, dict[str, Any]] = {}
        for tc in tool_calls:
            fn = tc.get("function") or {}
            tool_name = fn.get("name", "")
            args_str = fn.get("arguments") or "{}"
            try:
                norm_args = json.dumps(json.loads(args_str), sort_keys=True)
            except json.JSONDecodeError:
                norm_args = args_str
            key = f"{tool_name}::{norm_args}"

            if key in in_round_cache:
                # Duplicate within the same LLM round → reuse the shape.
                merged = _replay_tool_message(
                    st, tc, in_round_cache[key], tenant_id, model
                )
                messages.append(merged)
            else:
                handled = await _handle_tool_call(st, tc, model, tenant_id)
                in_round_cache[key] = handled
                messages.append(handled)

    # Hit the loop guard — return whatever we have plus a warning.
    log.warning("exceeded MAX_TOOL_ROUNDS=%d for tenant=%s", MAX_TOOL_ROUNDS, tenant_id)
    return response  # type: ignore[possibly-unbound]


async def _call_upstream(
    st: AppState,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    original_body: dict[str, Any],
    model: str,
    *,
    header_base_url: str | None = None,
    header_api_key: str | None = None,
) -> dict[str, Any]:
    """Call the real OpenAI-compatible upstream OR the mock LLM.

    The destination is resolved per request by :class:`UpstreamRouter`: an
    explicit ``X-Plynf-Upstream`` header wins, else a ``provider/model`` prefix
    routes to a configured provider (with the prefix stripped from the model the
    upstream sees), else the default ``upstream_base_url``. When nothing routes
    (demo mode, or no upstream configured at all) we return the deterministic
    mock so an offline / keyless proxy still works.
    """
    if st.settings.demo_mode:
        return mock_completion(messages, model=model, tools=tools)

    target = st.upstream_router.resolve(
        model, header_base_url=header_base_url, header_api_key=header_api_key
    )
    if not target.is_real:
        return mock_completion(messages, model=model, tools=tools)

    payload = dict(original_body)
    payload["messages"] = messages
    if tools is not None:
        payload["tools"] = tools
    # Show the upstream its native model id (provider prefix stripped). Only
    # rewrite when the caller actually sent a model, so we never inject one that
    # wasn't in the original request.
    if "model" in payload:
        payload["model"] = target.model

    headers = {
        "Authorization": f"Bearer {target.api_key}" if target.api_key else "",
        "Content-Type": "application/json",
    }
    headers = {k: v for k, v in headers.items() if v}

    url = target.chat_completions_url()
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, json=payload, headers=headers)
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"upstream error: {resp.text[:500]}",
        )
    return resp.json()


async def _forward_upstream(
    st: AppState,
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Proxy an un-shaped request verbatim to the configured OpenAI upstream.

    Backs the drop-in-completeness endpoints (``/v1/models``,
    ``/v1/embeddings``) — Plynf adds no value to these payloads but must expose
    them so a client pointed entirely at Plynf keeps working. Uses Plynf's own
    upstream key, mirroring :func:`_call_upstream`.
    """
    url = st.settings.upstream_base_url.rstrip("/") + path
    key = st.settings.upstream_api_key
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    async with httpx.AsyncClient(timeout=60) as client:
        if method.upper() == "GET":
            resp = await client.get(url, headers=headers)
        else:
            resp = await client.request(method.upper(), url, json=json_body, headers=headers)
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"upstream error: {resp.text[:500]}",
        )
    return resp.json()


async def _handle_tool_call(
    st: AppState,
    tool_call: dict[str, Any],
    model: str,
    tenant_id: str,
) -> dict[str, Any]:
    """Execute a single tool call, apply policy, log savings."""
    fn = tool_call.get("function") or {}
    tool_name = fn.get("name", "")
    args_str = fn.get("arguments") or "{}"
    try:
        args = json.loads(args_str)
    except json.JSONDecodeError:
        args = {}

    if not st.registry.has(tool_name):
        # Unknown tool → return a tool message saying so. The client (or LLM)
        # can decide what to do.
        return _tool_message(
            tool_call.get("id", ""),
            tool_name,
            {"error": f"tool '{tool_name}' is not registered with Plynf"},
        )

    connector_name = st.registry.resolve(tool_name) or "unknown"
    policy = _policy_for(st, connector_name, tool_name, tenant_id)

    # Cache lookup.
    cache_key = f"{tenant_id}:{connector_name}:{tool_name}:{json.dumps(args, sort_keys=True)}"
    hit, cached = st.cache.get(cache_key)
    if hit:
        raw = cached["raw"]
        shaped = cached["shaped"]
        cache_hit = True
    else:
        try:
            _conn, raw = await st.registry.execute(tool_name, args)
        except Exception as e:
            return _tool_message(
                tool_call.get("id", ""),
                tool_name,
                {"error": f"tool execution failed: {e!s}"},
            )
        try:
            shaped = apply(raw, policy)
        except PolicyError as pe:
            return _tool_message(
                tool_call.get("id", ""),
                tool_name,
                {"error": f"blocked by policy: {pe!s}"},
            )
        cache_hit = False
        if policy.cache_ttl:
            st.cache.set(cache_key, {"raw": raw, "shaped": shaped}, policy.cache_ttl)

    # Emit savings event.
    raw_tokens = count_json_tokens(raw)
    shaped_tokens = count_json_tokens(shaped)
    event = make_event(
        tenant_id=tenant_id,
        agent_id=None,
        connector=connector_name,
        tool=tool_name,
        model=model,
        raw_response_tokens=raw_tokens,
        shaped_response_tokens=shaped_tokens,
        cache_hit=cache_hit,
        request_args=args,
    )
    st.events.append(event)
    if st.sink is not None:
        st.sink.emit(event)

    return _tool_message(tool_call.get("id", ""), tool_name, shaped)


def _policy_for(
    st: AppState,
    connector: str,
    tool: str,
    tenant_id: str | None = None,
) -> ToolPolicy:
    cp = st.policies.get(connector)
    base = cp.policy_for(tool) if cp is not None else ToolPolicy(tool=tool)
    if tenant_id is None:
        return base
    override = st.overrides.get(tenant_id, connector, tool)
    return merge_override(base, override) if override else base


def _tool_message(tool_call_id: str, tool_name: str, content: Any) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "name": tool_name,
        "content": json.dumps(content),
    }


def _replay_tool_message(
    st: AppState,
    tc: dict[str, Any],
    original_message: dict[str, Any],
    tenant_id: str,
    model: str,
) -> dict[str, Any]:
    """Reuse a previously-shaped tool result for a duplicate tool_call.

    Emits a savings event marked ``cache_hit=True`` so the dashboard
    correctly attributes the in-round dedup to cross-call merging. The
    tool_call_id is replaced with the duplicate's id so OpenAI's schema
    invariant (each tool_call gets exactly one tool message) is preserved.
    """
    fn = tc.get("function") or {}
    tool_name = fn.get("name", "")
    args_str = fn.get("arguments") or "{}"
    try:
        args = json.loads(args_str)
    except json.JSONDecodeError:
        args = {}

    connector_name = st.registry.resolve(tool_name) or "unknown"
    # The original message stores the shaped JSON as a string. Recover it
    # for token counting; we don't re-shape (the policy was already applied).
    shaped_text = original_message.get("content", "{}")
    try:
        shaped_value = json.loads(shaped_text) if isinstance(shaped_text, str) else shaped_text
    except json.JSONDecodeError:
        shaped_value = shaped_text
    shaped_tokens = count_json_tokens(shaped_value)

    # On a merge, the alternative cost would have been another full raw
    # fetch + shape, so the "saved" tokens are the shaped tokens themselves
    # (they're a cache hit on the in-round shared result).
    event = make_event(
        tenant_id=tenant_id,
        agent_id=None,
        connector=connector_name,
        tool=tool_name,
        model=model,
        raw_response_tokens=shaped_tokens,
        shaped_response_tokens=shaped_tokens,
        cache_hit=True,  # treated as a cache hit for accounting
        request_args=args,
    )
    st.events.append(event)
    if st.sink is not None:
        try:
            st.sink.emit(event)
        except Exception:  # noqa: BLE001 — sink never crashes the request
            log.warning("savings sink failed on merged event", exc_info=True)

    return {
        "role": "tool",
        "tool_call_id": tc.get("id", ""),
        "name": tool_name,
        "content": shaped_text,
    }


async def _has_body(request: Request) -> bool:
    """Return True if the request has a non-empty body."""
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            return int(cl) > 0
        except ValueError:
            pass
    body = await request.body()
    return bool(body)


async def _synthesize_anthropic_sse(final: dict[str, Any]):
    """Yield Anthropic-shaped SSE events for a completed Anthropic message.

    Anthropic's stream taxonomy:

      message_start          → message metadata (no content)
      content_block_start    → opening a text or tool_use block
      content_block_delta    → incremental text_delta / input_json_delta
      content_block_stop     → closing the block
      message_delta          → final stop_reason + usage
      message_stop           → sentinel

    Each event has an ``event: <type>`` header line and a ``data: <json>``
    payload line, per the SSE protocol.
    """
    def _ev(event_type: str, payload: dict[str, Any]) -> str:
        return (
            f"event: {event_type}\n"
            f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
        )

    msg_id = final.get("id", "msg_unknown")
    model = final.get("model", "")
    usage_in = (final.get("usage") or {}).get("input_tokens", 0)
    usage_out = (final.get("usage") or {}).get("output_tokens", 0)

    # 1. message_start (with empty content; content_block events follow)
    yield _ev("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": model,
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": usage_in, "output_tokens": 0},
        },
    })

    # 2. per-content-block events
    blocks = final.get("content") or []
    for idx, block in enumerate(blocks):
        if block.get("type") == "text":
            text = block.get("text", "")
            yield _ev("content_block_start", {
                "type": "content_block_start",
                "index": idx,
                "content_block": {"type": "text", "text": ""},
            })
            # Word-by-word delta keeps the streaming UX believable.
            parts = text.split(" ")
            for j, part in enumerate(parts):
                piece = part if j == 0 else " " + part
                if not piece:
                    continue
                yield _ev("content_block_delta", {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {"type": "text_delta", "text": piece},
                })
            yield _ev("content_block_stop", {
                "type": "content_block_stop",
                "index": idx,
            })
        elif block.get("type") == "tool_use":
            yield _ev("content_block_start", {
                "type": "content_block_start",
                "index": idx,
                "content_block": {
                    "type": "tool_use",
                    "id": block.get("id"),
                    "name": block.get("name"),
                    "input": {},
                },
            })
            yield _ev("content_block_delta", {
                "type": "content_block_delta",
                "index": idx,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": json.dumps(block.get("input") or {}),
                },
            })
            yield _ev("content_block_stop", {
                "type": "content_block_stop",
                "index": idx,
            })

    # 3. message_delta — final stop_reason + output usage
    yield _ev("message_delta", {
        "type": "message_delta",
        "delta": {
            "stop_reason": final.get("stop_reason"),
            "stop_sequence": final.get("stop_sequence"),
        },
        "usage": {"output_tokens": usage_out},
    })
    # 4. message_stop sentinel
    yield _ev("message_stop", {"type": "message_stop"})


async def _synthesize_responses_sse(final: dict[str, Any]):
    """Yield OpenAI *Responses* typed SSE events for a completed response.

    The Responses stream is a typed-event taxonomy, not chat ``chunk`` deltas:
    a ``response.created`` envelope (status ``in_progress``, empty output), then
    per-output-item lifecycle events — ``output_item.added`` →
    (``content_part.added`` → ``output_text.delta``\\* → ``output_text.done`` →
    ``content_part.done``) for a message, or ``function_call_arguments.delta`` /
    ``.done`` for a tool call — each closed by ``output_item.done``, and finally
    ``response.completed`` carrying the full body. Plynf computed the result
    unary (tool-call interception must finish first), so we replay the final
    ``output`` array as that sequence: text split word-by-word, tool-call
    arguments streamed whole. Every event carries a monotonic
    ``sequence_number`` as the real API does.
    """
    seq = 0

    def _ev(event_type: str, payload: dict[str, Any]) -> str:
        nonlocal seq
        data = {"type": event_type, "sequence_number": seq, **payload}
        seq += 1
        return f"event: {event_type}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"

    # Skeleton: the response object before any output is produced.
    skeleton = {**final, "status": "in_progress", "output": [], "output_text": ""}
    yield _ev("response.created", {"response": skeleton})
    yield _ev("response.in_progress", {"response": skeleton})

    for output_index, item in enumerate(final.get("output") or []):
        itype = item.get("type")
        item_id = item.get("id", "")

        if itype == "message":
            shell = {**item, "status": "in_progress", "content": []}
            yield _ev(
                "response.output_item.added",
                {"output_index": output_index, "item": shell},
            )
            for content_index, part in enumerate(item.get("content") or []):
                if part.get("type") != "output_text":
                    continue
                text = part.get("text") or ""
                loc = {
                    "item_id": item_id,
                    "output_index": output_index,
                    "content_index": content_index,
                }
                yield _ev(
                    "response.content_part.added",
                    {**loc, "part": {"type": "output_text", "text": "", "annotations": []}},
                )
                for i, piece in enumerate(text.split(" ")):
                    frag = piece if i == 0 else " " + piece
                    if frag:
                        yield _ev("response.output_text.delta", {**loc, "delta": frag})
                yield _ev("response.output_text.done", {**loc, "text": text})
                yield _ev(
                    "response.content_part.done",
                    {**loc, "part": {"type": "output_text", "text": text, "annotations": []}},
                )
            yield _ev(
                "response.output_item.done",
                {"output_index": output_index, "item": item},
            )

        elif itype == "function_call":
            shell = {**item, "status": "in_progress", "arguments": ""}
            yield _ev(
                "response.output_item.added",
                {"output_index": output_index, "item": shell},
            )
            args = item.get("arguments") or ""
            if args:
                yield _ev(
                    "response.function_call_arguments.delta",
                    {"item_id": item_id, "output_index": output_index, "delta": args},
                )
            yield _ev(
                "response.function_call_arguments.done",
                {"item_id": item_id, "output_index": output_index, "arguments": args},
            )
            yield _ev(
                "response.output_item.done",
                {"output_index": output_index, "item": item},
            )

    yield _ev("response.completed", {"response": final})


async def _synthesize_cohere_sse(final: dict[str, Any]):
    """Yield Cohere v2-shaped SSE events for a completed chat response.

    Cohere v2 streaming is a typed-event taxonomy carried as ``data: {json}``
    SSE, with the event kind in each payload's ``type`` field: ``message-start``
    → (``content-start`` → ``content-delta``\\* → ``content-end``) for assistant
    text and (``tool-call-start`` → ``tool-call-delta`` → ``tool-call-end``) for
    each tool call, closed by ``message-end`` carrying ``finish_reason`` +
    ``usage``. Plynf computed the result unary (tool-call interception must
    finish first), so we replay the shaped final ``message`` as that sequence —
    text split word-by-word, tool-call arguments streamed whole.
    """

    def _data(payload: dict[str, Any]) -> str:
        return "data: " + json.dumps(payload, separators=(",", ":")) + "\n\n"

    message = final.get("message") or {}

    # message-start: an empty assistant message shell.
    yield _data(
        {
            "id": final.get("id", ""),
            "type": "message-start",
            "delta": {
                "message": {
                    "role": "assistant",
                    "content": [],
                    "tool_plan": "",
                    "tool_calls": [],
                }
            },
        }
    )

    # Text content blocks stream first (Cohere emits assistant text before
    # tool calls, mirroring OpenAI's generation order).
    index = 0
    for block in message.get("content") or []:
        if block.get("type") != "text":
            continue
        text = block.get("text") or ""
        yield _data(
            {
                "type": "content-start",
                "index": index,
                "delta": {"message": {"content": {"type": "text", "text": ""}}},
            }
        )
        for i, piece in enumerate(text.split(" ")):
            frag = piece if i == 0 else " " + piece
            if frag:
                yield _data(
                    {
                        "type": "content-delta",
                        "index": index,
                        "delta": {"message": {"content": {"text": frag}}},
                    }
                )
        yield _data({"type": "content-end", "index": index})
        index += 1

    # Tool calls: a start/delta/end triple each, arguments streamed whole.
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        yield _data(
            {
                "type": "tool-call-start",
                "index": index,
                "delta": {
                    "message": {
                        "tool_calls": {
                            "id": tc.get("id", ""),
                            "type": "function",
                            "function": {"name": fn.get("name", ""), "arguments": ""},
                        }
                    }
                },
            }
        )
        args = fn.get("arguments") or ""
        if args:
            yield _data(
                {
                    "type": "tool-call-delta",
                    "index": index,
                    "delta": {"message": {"tool_calls": {"function": {"arguments": args}}}},
                }
            )
        yield _data({"type": "tool-call-end", "index": index})
        index += 1

    # message-end: finish_reason + usage on the closing event.
    yield _data(
        {
            "type": "message-end",
            "delta": {
                "finish_reason": final.get("finish_reason", "COMPLETE"),
                "usage": final.get("usage") or {},
            },
        }
    )


def _aws_eventstream_frame(event_type: str, payload: dict[str, Any]) -> bytes:
    """Encode one AWS ``vnd.amazon.eventstream`` binary message.

    Wire layout (all integers big-endian) — what boto3 / the AWS SDKs decode::

        prelude  : total_len(u32) headers_len(u32) prelude_crc(u32 = CRC32 of
                   the 8 prelude bytes)
        headers  : repeated [name_len(u8) name :type(u8) ...]; here every
                   header is a string (type 7): val_len(u16) val
        payload  : the JSON bytes
        crc      : u32 = CRC32 of the whole message *excluding* these 4 bytes

    The three standard event headers are ``:event-type`` (the discriminator),
    ``:content-type`` (``application/json``) and ``:message-type`` (``event``).
    """
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    headers = bytearray()
    for name, value in (
        (":event-type", event_type),
        (":content-type", "application/json"),
        (":message-type", "event"),
    ):
        name_bytes = name.encode("utf-8")
        value_bytes = value.encode("utf-8")
        headers.append(len(name_bytes))
        headers.extend(name_bytes)
        headers.append(7)  # value type 7 == UTF-8 string
        headers.extend(struct.pack(">H", len(value_bytes)))
        headers.extend(value_bytes)

    total_len = 4 + 4 + 4 + len(headers) + len(body) + 4
    prelude = struct.pack(">II", total_len, len(headers))
    prelude += struct.pack(">I", binascii.crc32(prelude) & 0xFFFFFFFF)
    message = prelude + bytes(headers) + body
    return message + struct.pack(">I", binascii.crc32(message) & 0xFFFFFFFF)


async def _synthesize_bedrock_converse_stream(final: dict[str, Any]):
    """Yield Bedrock ``ConverseStream`` binary event-stream frames.

    Bedrock streaming is not SSE — it is the AWS event-stream binary protocol
    (see :func:`_aws_eventstream_frame`). The event sequence: ``messageStart``
    (role), then per content block — text blocks emit ``contentBlockDelta``
    (``delta.text``, word-by-word) then ``contentBlockStop``; ``toolUse`` blocks
    emit ``contentBlockStart`` (``start.toolUse`` id + name), a single
    ``contentBlockDelta`` whose ``delta.toolUse.input`` is the arguments
    serialized as a JSON *string* (how Bedrock streams tool input), then
    ``contentBlockStop`` — and finally ``messageStop`` (``stopReason``) and a
    ``metadata`` event (``usage`` + ``metrics``). Plynf computed the result
    unary, so this just reframes the shaped final body.
    """
    message = (final.get("output") or {}).get("message") or {}

    yield _aws_eventstream_frame("messageStart", {"role": message.get("role", "assistant")})

    for index, block in enumerate(message.get("content") or []):
        if "text" in block:
            text = block.get("text") or ""
            for i, piece in enumerate(text.split(" ")):
                frag = piece if i == 0 else " " + piece
                if frag:
                    yield _aws_eventstream_frame(
                        "contentBlockDelta",
                        {"contentBlockIndex": index, "delta": {"text": frag}},
                    )
            yield _aws_eventstream_frame("contentBlockStop", {"contentBlockIndex": index})
        elif "toolUse" in block:
            tool_use = block["toolUse"]
            yield _aws_eventstream_frame(
                "contentBlockStart",
                {
                    "contentBlockIndex": index,
                    "start": {
                        "toolUse": {
                            "toolUseId": tool_use.get("toolUseId", ""),
                            "name": tool_use.get("name", ""),
                        }
                    },
                },
            )
            input_json = json.dumps(tool_use.get("input") or {}, separators=(",", ":"))
            yield _aws_eventstream_frame(
                "contentBlockDelta",
                {"contentBlockIndex": index, "delta": {"toolUse": {"input": input_json}}},
            )
            yield _aws_eventstream_frame("contentBlockStop", {"contentBlockIndex": index})

    yield _aws_eventstream_frame(
        "messageStop", {"stopReason": final.get("stopReason", "end_turn")}
    )
    yield _aws_eventstream_frame(
        "metadata",
        {"usage": final.get("usage") or {}, "metrics": final.get("metrics") or {}},
    )


async def _synthesize_gemini_sse(final: dict[str, Any]):
    """Yield Gemini-shaped SSE chunks for a completed ``candidates`` response.

    Mirrors the OpenAI/Anthropic synthesizers: the request ran unary (tool-call
    interception always completes synchronously), then the final candidate is
    re-emitted as ``data: {GenerateContentResponse}`` chunks — text split
    word-by-word for a realistic stream, any ``functionCall`` part emitted
    whole, and a terminal chunk carrying ``finishReason`` + ``usageMetadata``,
    matching how Gemini frames ``streamGenerateContent?alt=sse``.
    """

    def _data(payload: dict[str, Any]) -> str:
        return "data: " + json.dumps(payload, separators=(",", ":")) + "\n\n"

    candidate = (final.get("candidates") or [{}])[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    model_version = final.get("modelVersion", "")

    def _chunk(
        chunk_parts: list[dict[str, Any]],
        *,
        finish: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        cand: dict[str, Any] = {
            "content": {"role": "model", "parts": chunk_parts},
            "index": 0,
        }
        if finish is not None:
            cand["finishReason"] = finish
        payload: dict[str, Any] = {"candidates": [cand], "modelVersion": model_version}
        if usage is not None:
            payload["usageMetadata"] = usage
        return payload

    for part in parts:
        if "text" in part:
            pieces = (part.get("text") or "").split(" ")
            for i, piece in enumerate(pieces):
                frag = piece if i == 0 else " " + piece
                if frag:
                    yield _data(_chunk([{"text": frag}]))
        else:
            # functionCall (or any non-text part) streams as one whole chunk.
            yield _data(_chunk([part]))

    # Terminal chunk: finishReason + usageMetadata on an empty-parts candidate.
    yield _data(
        _chunk(
            [],
            finish=candidate.get("finishReason", "STOP"),
            usage=final.get("usageMetadata") or {},
        )
    )


async def _synthesize_sse(final: dict[str, Any]):
    """Yield OpenAI-shaped SSE chunks for a completed chat response.

    We chunk the assistant content word-by-word so existing clients (the
    OpenAI SDK, LangChain streaming hooks) see realistic streaming behaviour.
    Tool-call traffic doesn't appear here — by the time we reach this point,
    Plynf has already executed and shaped every tool call in the round-trip.
    """
    cid = final.get("id", "chatcmpl-plynf")
    model = final.get("model", "")
    created = final.get("created", 0)
    choice = (final.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = message.get("content") or ""
    finish_reason = choice.get("finish_reason", "stop")

    def _chunk(delta: dict[str, Any], finish: str | None = None) -> str:
        payload = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return "data: " + json.dumps(payload, separators=(",", ":")) + "\n\n"

    # 1. role chunk
    yield _chunk({"role": "assistant"})
    # 2. content chunks (split on whitespace for visible streaming UX)
    if content:
        parts = content.split(" ")
        for i, part in enumerate(parts):
            piece = part if i == 0 else " " + part
            yield _chunk({"content": piece})
    # 3. terminator
    yield _chunk({}, finish=finish_reason)
    yield "data: [DONE]\n\n"


# Default app for ``uvicorn plinth_proxy.api:app``.
app = create_app()


__all__ = ["app", "create_app", "count_messages_tokens"]
