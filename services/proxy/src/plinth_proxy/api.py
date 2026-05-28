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
  GET  /v1/savings/summary    aggregate dashboard view
  GET  /v1/policies           list loaded connector policies
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import __version__
from .anthropic_adapter import anthropic_request_to_openai, openai_response_to_anthropic
from .cache import TTLCache
from .connectors import (
    TOOL_TO_CONNECTOR,
    ConnectorRegistry,
    make_mock_registry,
)
from .gateway_client import GatewayClient, make_gateway_registry
from .identity_client import IdentityClient, IdentityError
from .mock_llm import mock_completion
from .policy_engine import ConnectorPolicy, PolicyError, ToolPolicy, apply, load_all_policies
from .postgres_sink import PostgresSavingsSink
from .savings import SavingsEvent, SavingsSink, aggregate, make_event
from .settings import ProxySettings
from .tier_gate import TierGate, upgrade_hint
from .tokens import count_json_tokens, count_messages_tokens

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


def create_app(settings: ProxySettings | None = None) -> FastAPI:
    """Application factory. Allows tests to inject custom settings."""
    app = FastAPI(title="Plynf LLM-Proxy", version=__version__, lifespan=_lifespan)
    if settings is not None:
        app.state.plinth = _build_state(settings)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        st: AppState = app.state.plinth
        return {
            "status": "ok",
            "version": __version__,
            "policies_loaded": len(st.policies),
            "demo_mode": st.settings.demo_mode,
        }

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

    @app.get("/v1/savings/summary")
    async def savings_summary() -> dict[str, Any]:
        st: AppState = app.state.plinth
        return aggregate(st.events)

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

        if tool_name not in TOOL_TO_CONNECTOR:
            raise HTTPException(status_code=404, detail=f"unknown tool: {tool_name}")

        body = await request.json() if await _has_body(request) else {}
        args = body.get("arguments") or {}
        agent_id = body.get("agent_id")
        workflow_id = body.get("workflow_id")

        connector_name = TOOL_TO_CONNECTOR[tool_name]
        policy = _policy_for(st, connector_name, tool_name)

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

        if tool_name not in TOOL_TO_CONNECTOR:
            # Unknown tool → pass through. Caller can still use Plynf for
            # the tools that do have policies.
            return JSONResponse({"shaped": raw, "shaped_by_plynf": False})

        connector_name = TOOL_TO_CONNECTOR[tool_name]
        policy = _policy_for(st, connector_name, tool_name)

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
        tenant_id, tier = await _authenticate(request, st)
        _enforce_tier(st, tenant_id, tier)

        if anth_body.get("stream"):
            raise HTTPException(
                status_code=501,
                detail="streaming not yet supported on /v1/messages",
            )

        openai_body = anthropic_request_to_openai(anth_body)
        before_count = len(st.events)
        openai_final = await _handle_chat(st, openai_body, tenant_id)
        new_shaped = sum(
            ev.shaped_response_tokens for ev in st.events[before_count:]
        )
        if new_shaped:
            st.gate.record_tokens(tenant_id, new_shaped)

        return JSONResponse(openai_response_to_anthropic(openai_final))

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        st: AppState = app.state.plinth
        body = await request.json()
        tenant_id, tier = await _authenticate(request, st)
        _enforce_tier(st, tenant_id, tier)
        wants_stream = bool(body.get("stream"))
        # Tool-call interception requires holding the response until we've
        # shaped and re-called, so we always run the full flow first, then
        # synthesize SSE chunks for streaming clients. Same OpenAI contract,
        # same content — trades first-token-latency for tool-call correctness.
        before_count = len(st.events)
        final = await _handle_chat(st, body, tenant_id)
        # Charge the tenant for the shaped tokens that just flowed through.
        new_shaped_tokens = sum(
            ev.shaped_response_tokens for ev in st.events[before_count:]
        )
        if new_shaped_tokens:
            st.gate.record_tokens(tenant_id, new_shaped_tokens)

        if not wants_stream:
            return JSONResponse(final)
        return StreamingResponse(
            _synthesize_sse(final),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


# ---------------------------------------------------------------------------
# Authentication (minimal)
# ---------------------------------------------------------------------------


async def _authenticate(request: Request, st: AppState) -> tuple[str, str]:
    """Resolve ``Authorization: Bearer <token>`` → ``(tenant_id, tier)``.

    Resolution order:

      1. **Static api_keys map** — fast path for self-hosted / demo setups.
         Configured via ``PLINTH_PROXY_API_KEYS``.
      2. **Identity service JWT verify** — if ``PLINTH_PROXY_IDENTITY_URL`` is
         set, the token is forwarded to ``/v1/tokens/verify`` and the
         tenant_id + tier are read off the signed claims.
      3. **Open mode** — no api_keys, no identity URL → caller is labelled
         ``demo`` at the configured ``demo_tier`` (default ``enterprise``).

    Returns ``(tenant_id, tier)`` or raises ``401``.
    """
    auth = request.headers.get("authorization", "")

    # 1. Static map fast-path.
    if st.api_keys:
        if not auth.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="missing bearer token")
        key = auth.split(" ", 1)[1].strip()
        tenant = st.api_keys.get(key)
        if tenant is not None:
            return tenant, st.api_key_tiers.get(key, "free")
        # Static map present but key didn't match — fall through to identity
        # if configured, otherwise 401.
        if st.identity is None:
            raise HTTPException(status_code=401, detail="unknown api key")

    # 2. Identity-service verify.
    if st.identity is not None:
        if not auth.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="missing bearer token")
        token = auth.split(" ", 1)[1].strip()
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
# Chat-completion handler
# ---------------------------------------------------------------------------


async def _handle_chat(st: AppState, body: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    messages: list[dict[str, Any]] = list(body.get("messages") or [])
    tools: list[dict[str, Any]] | None = body.get("tools")
    model: str = body.get("model") or st.settings.default_model

    for _round in range(MAX_TOOL_ROUNDS):
        response = await _call_upstream(st, messages, tools, body, model)

        choice = (response.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        tool_calls = message.get("tool_calls") or []

        if not tool_calls:
            # Final answer — return as-is.
            return response

        # Append the assistant message so the next round has full history.
        messages.append(message)

        # Execute each tool call we recognise. Unknown tools get a "tool"
        # message saying we couldn't handle it — the LLM may retry or give up.
        for tc in tool_calls:
            handled = await _handle_tool_call(st, tc, model, tenant_id)
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
) -> dict[str, Any]:
    """Call the real OpenAI upstream OR the mock LLM."""
    if not st.settings.upstream_base_url or st.settings.demo_mode:
        return mock_completion(messages, model=model, tools=tools)

    payload = dict(original_body)
    payload["messages"] = messages
    if tools is not None:
        payload["tools"] = tools

    upstream_key = st.settings.upstream_api_key
    headers = {
        "Authorization": f"Bearer {upstream_key}" if upstream_key else "",
        "Content-Type": "application/json",
    }
    headers = {k: v for k, v in headers.items() if v}

    url = st.settings.upstream_base_url.rstrip("/") + "/v1/chat/completions"
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, json=payload, headers=headers)
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

    connector_name = TOOL_TO_CONNECTOR[tool_name]
    policy = _policy_for(st, connector_name, tool_name)

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


def _policy_for(st: AppState, connector: str, tool: str) -> ToolPolicy:
    cp = st.policies.get(connector)
    if cp is None:
        return ToolPolicy(tool=tool)
    return cp.policy_for(tool)


def _tool_message(tool_call_id: str, tool_name: str, content: Any) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "name": tool_name,
        "content": json.dumps(content),
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
