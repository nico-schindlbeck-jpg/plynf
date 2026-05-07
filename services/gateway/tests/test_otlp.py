# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the OTLP emitter + ``/v1/observability`` endpoints."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from plinth_gateway.api import create_app
from plinth_gateway.audit import AuditLog, AuditRecord
from plinth_gateway.otlp_emitter import (
    OTLPEmitter,
    _build_attributes,
    _event_severity,
    _flatten_attributes,
    _parse_event_timestamp,
)
from plinth_gateway.settings import Settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeExporter:
    """Minimal in-memory exporter that drains the OTel batch processor.

    The real ``InMemoryLogExporter`` works too — we keep this around so tests
    that *don't* care about the OTel SDK internals stay easy to read.
    """

    def __init__(self) -> None:
        self.records: list[Any] = []
        self.shutdown_called = False
        self.fail_on_export = False
        from opentelemetry.sdk._logs.export import LogExportResult

        self._success = LogExportResult.SUCCESS
        self._failure = LogExportResult.FAILURE

    def export(self, batch):
        if self.fail_on_export:
            return self._failure
        self.records.extend(batch)
        return self._success

    def shutdown(self):
        self.shutdown_called = True

    def force_flush(self, timeout_millis: int = 30000) -> bool:  # noqa: ARG002
        return True


def _settings(**overrides: Any) -> Settings:
    base = {
        "data_dir": "/tmp/plinth-otlp-test",
        "gateway_host": "127.0.0.1",
        "gateway_port": 7422,
        "log_level": "WARNING",
        "log_format": "console",
        "backend_timeout_seconds": 5.0,
        "otlp_enabled": True,
        "otlp_endpoint": "http://localhost:4318",
        "otlp_service_name": "plinth-gateway",
        "otlp_batch_size": 8,
        "otlp_flush_interval_seconds": 0.05,
        "otlp_headers_json": "{}",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _audit_event_dict(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": "evt_01HZX9V5K4M7N0Q1R2S3T4V5W6",
        "timestamp": "2026-05-07T16:30:00+00:00",
        "tool_id": "web.fetch",
        "workspace_id": "ws_01HZX9V5K4M7N0Q1R2S3T4V5W6",
        "agent_id": "ag_test",
        "tenant_id": "default",
        "arguments_hash": "a" * 64,
        "arguments_preview": '{"url":"mock://example"}',
        "result_hash": "b" * 64,
        "cached": False,
        "duration_ms": 142,
        "cost_estimate_usd": 0.0023,
        "error": None,
        "type": "tool.invoked",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Pure-unit tests — flattening, attributes, severity, timestamp
# ---------------------------------------------------------------------------


def test_flatten_attributes_basic_primitives() -> None:
    out = _flatten_attributes({"a": 1, "b": "x", "c": True, "d": 1.5})
    assert out == {"a": 1, "b": "x", "c": True, "d": 1.5}


def test_flatten_attributes_skips_none_values() -> None:
    out = _flatten_attributes({"present": "yes", "missing": None})
    assert out == {"present": "yes"}
    assert "missing" not in out


def test_flatten_attributes_dot_paths_for_nested_dict() -> None:
    out = _flatten_attributes({"actor": {"kind": "agent", "id": "a1"}})
    assert out == {"actor.kind": "agent", "actor.id": "a1"}


def test_flatten_attributes_homogeneous_list_passes_through() -> None:
    out = _flatten_attributes({"scopes": ["read", "write"]})
    assert out["scopes"] == ["read", "write"]


def test_flatten_attributes_mixed_list_becomes_json_string() -> None:
    out = _flatten_attributes({"items": [{"k": "v"}, {"k": "w"}]})
    assert isinstance(out["items"], str)
    assert json.loads(out["items"]) == [{"k": "v"}, {"k": "w"}]


def test_build_attributes_full_event_namespacing() -> None:
    event = _audit_event_dict()
    attrs = _build_attributes(event, "plinth-gateway")
    assert attrs["service.name"] == "plinth-gateway"
    assert attrs["tool.id"] == "web.fetch"
    assert attrs["tool.cached"] is False
    assert attrs["tool.duration_ms"] == 142
    assert attrs["tool.cost_usd"] == pytest.approx(0.0023)
    assert attrs["agent.id"] == "ag_test"
    assert attrs["tenant.id"] == "default"
    assert attrs["workspace.id"].startswith("ws_")
    assert attrs["arguments.hash"] == "a" * 64
    assert attrs["arguments.preview"].startswith('{"url":')
    assert attrs["audit.id"].startswith("evt_")
    assert "error.message" not in attrs


def test_build_attributes_truncates_long_preview() -> None:
    long_preview = "x" * 5000
    event = _audit_event_dict(arguments_preview=long_preview)
    attrs = _build_attributes(event, "plinth-gateway")
    assert len(attrs["arguments.preview"]) == 500


def test_build_attributes_includes_error_when_present() -> None:
    event = _audit_event_dict(error="boom")
    attrs = _build_attributes(event, "plinth-gateway")
    assert attrs["error.message"] == "boom"


def test_event_severity_info_for_success() -> None:
    from opentelemetry._logs import SeverityNumber

    sev_n, sev_t = _event_severity(_audit_event_dict())
    assert sev_n == SeverityNumber.INFO
    assert sev_t == "INFO"


def test_event_severity_error_for_failure() -> None:
    from opentelemetry._logs import SeverityNumber

    sev_n, sev_t = _event_severity(_audit_event_dict(error="boom"))
    assert sev_n == SeverityNumber.ERROR
    assert sev_t == "ERROR"


def test_parse_event_timestamp_iso_string() -> None:
    ts = _parse_event_timestamp({"timestamp": "2026-05-07T12:00:00Z"})
    # Roughly mid-2026 → year 2026 in the date.
    parsed = datetime.fromtimestamp(ts / 1_000_000_000, tz=timezone.utc)
    assert parsed.year == 2026


def test_parse_event_timestamp_falls_back_for_garbage() -> None:
    ts = _parse_event_timestamp({"timestamp": "not a timestamp"})
    # Very recent (within 5s of now).
    now = datetime.now(timezone.utc).timestamp() * 1_000_000_000
    assert abs(ts - now) < 5_000_000_000


def test_parse_event_timestamp_handles_datetime_object() -> None:
    target = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ts = _parse_event_timestamp({"timestamp": target})
    assert ts == int(target.timestamp() * 1_000_000_000)


# ---------------------------------------------------------------------------
# Emitter behaviour
# ---------------------------------------------------------------------------


def test_emitter_disabled_is_noop() -> None:
    settings = _settings(otlp_enabled=False)
    emitter = OTLPEmitter(settings)
    assert emitter.enabled is False
    emitter.emit(_audit_event_dict())
    assert emitter._events_emitted == 0
    assert emitter.status["otlp_enabled"] is False
    assert emitter.status["otlp_endpoint"] is None


@pytest.mark.asyncio
async def test_emitter_start_stop_is_idempotent_when_disabled() -> None:
    emitter = OTLPEmitter(_settings(otlp_enabled=False))
    await emitter.start()
    await emitter.start()  # second start is a no-op
    await emitter.stop()
    await emitter.stop()  # safe to stop twice


@pytest.mark.asyncio
async def test_emitter_emits_to_in_memory_exporter() -> None:
    emitter = OTLPEmitter(_settings())
    fake = _FakeExporter()
    await emitter.start(exporter=fake)

    emitter.emit(_audit_event_dict())
    assert emitter._events_emitted == 1
    assert emitter._last_emit_at is not None

    await emitter.flush()
    await emitter.stop()

    # Exporter should have seen the encoded log.
    assert fake.shutdown_called is True
    assert len(fake.records) == 1
    record = fake.records[0]
    # ``record.log_record`` carries the actual log record with attributes.
    log_record = record.log_record
    assert log_record.body == "tool.invoked"
    attrs = dict(log_record.attributes or {})
    assert attrs["tool.id"] == "web.fetch"
    assert attrs["service.name"] == "plinth-gateway"


@pytest.mark.asyncio
async def test_emitter_emit_does_not_raise_when_logger_broken() -> None:
    """If the OTel logger throws inside ``emit`` we still don't crash."""
    emitter = OTLPEmitter(_settings())
    await emitter.start(exporter=_FakeExporter())

    class _BoomLogger:
        def emit(self, _record):
            raise RuntimeError("boom")

    emitter._logger = _BoomLogger()  # type: ignore[assignment]
    emitter.emit(_audit_event_dict())
    assert emitter._flush_errors == 1
    assert emitter._events_emitted == 0


@pytest.mark.asyncio
async def test_emitter_status_shape_when_enabled() -> None:
    emitter = OTLPEmitter(_settings())
    await emitter.start(exporter=_FakeExporter())
    emitter.emit(_audit_event_dict())
    s = emitter.status
    assert s["otlp_enabled"] is True
    assert s["otlp_endpoint"] == "http://localhost:4318"
    assert s["events_emitted"] == 1
    assert s["last_emit_at"] is not None
    assert s["flush_errors"] == 0
    await emitter.stop()


@pytest.mark.asyncio
async def test_emitter_invalid_headers_json_does_not_crash() -> None:
    emitter = OTLPEmitter(_settings(otlp_headers_json="not-json"))
    assert emitter.headers == {}


@pytest.mark.asyncio
async def test_emitter_valid_headers_json_parsed() -> None:
    emitter = OTLPEmitter(
        _settings(otlp_headers_json='{"Authorization": "Bearer abc"}')
    )
    assert emitter.headers == {"Authorization": "Bearer abc"}


@pytest.mark.asyncio
async def test_emitter_start_failure_disables_silently(monkeypatch) -> None:
    """If LoggerProvider construction blows up, OTLP self-disables."""

    def _boom(*_a, **_kw):
        raise RuntimeError("provider boom")

    import plinth_gateway.otlp_emitter as mod

    # Patch the lazy import via the SDK module path the emitter uses.
    monkeypatch.setattr(
        "opentelemetry.sdk._logs.LoggerProvider",
        _boom,
    )

    emitter = OTLPEmitter(_settings())
    await emitter.start()
    assert emitter.enabled is False
    assert emitter._flush_errors >= 1

    # And emit() is a no-op now.
    emitter.emit(_audit_event_dict())
    assert emitter._events_emitted == 0

    # Reference unused import for static analysis silencing.
    _ = mod


# ---------------------------------------------------------------------------
# Audit-log integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_record_emits_when_emitter_attached(db) -> None:
    fake = _FakeExporter()
    emitter = OTLPEmitter(_settings())
    await emitter.start(exporter=fake)
    audit = AuditLog(db, otlp=emitter)

    rec = AuditRecord(
        tool_id="web.fetch",
        arguments={"url": "mock://x"},
        workspace_id="ws_a",
        agent_id="ag_a",
        arguments_hash="h" * 64,
        arguments_preview='{"url":"mock://x"}',
        cached=False,
        duration_ms=42,
        cost_estimate_usd=0.001,
        result_hash="r" * 64,
    )
    event = await audit.record(rec)
    assert event.id.startswith("evt_")
    await emitter.flush()
    await emitter.stop()
    assert emitter._events_emitted == 1
    assert len(fake.records) == 1


@pytest.mark.asyncio
async def test_audit_record_works_without_emitter(db) -> None:
    """Back-compat: AuditLog without an emitter behaves exactly like v0.3."""
    audit = AuditLog(db)  # no otlp= kwarg
    rec = AuditRecord(
        tool_id="web.fetch",
        arguments={},
        workspace_id="ws_a",
        agent_id="ag_a",
        arguments_hash="h" * 64,
        arguments_preview="{}",
        cached=False,
        duration_ms=1,
        cost_estimate_usd=0.0,
        result_hash="r" * 64,
    )
    event = await audit.record(rec)
    assert event.id.startswith("evt_")


@pytest.mark.asyncio
async def test_emitter_failure_does_not_break_audit_record(db) -> None:
    """An emitter that throws on .emit() must not break audit persistence."""

    class _ExplodingEmitter:
        def emit(self, _payload):
            raise RuntimeError("collector down")

    audit = AuditLog(db, otlp=_ExplodingEmitter())  # type: ignore[arg-type]
    rec = AuditRecord(
        tool_id="web.fetch",
        arguments={},
        workspace_id="ws_a",
        agent_id="ag_a",
        arguments_hash="h" * 64,
        arguments_preview="{}",
        cached=False,
        duration_ms=1,
        cost_estimate_usd=0.0,
        result_hash="r" * 64,
    )
    event = await audit.record(rec)
    assert event.id.startswith("evt_")
    # And the row persisted.
    rows = await audit.query(workspace_id="ws_a")
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# /v1/observability endpoints
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def otlp_client(tmp_path):
    """Spin up a gateway app with OTLP DISABLED (default settings).

    Most endpoint tests don't need a real exporter — they just hit the
    status/flush surfaces.
    """
    settings = Settings(
        data_dir=tmp_path,
        gateway_host="127.0.0.1",
        gateway_port=7422,
        log_level="WARNING",
        log_format="console",
        backend_timeout_seconds=5.0,
        otlp_enabled=False,
    )
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": "Bearer test-token"},
    ) as client, app.router.lifespan_context(app):
        yield app, client


@pytest.mark.asyncio
async def test_status_endpoint_when_disabled(otlp_client) -> None:
    _, client = otlp_client
    r = await client.get("/v1/observability/status")
    assert r.status_code == 200
    body = r.json()
    assert body["otlp_enabled"] is False
    assert body["otlp_endpoint"] is None
    assert body["events_emitted"] == 0
    assert body["flush_errors"] == 0
    assert body["last_emit_at"] is None


@pytest.mark.asyncio
async def test_status_endpoint_reports_after_emit(tmp_path) -> None:
    """When enabled with a fake exporter, status reflects events emitted."""
    settings = Settings(
        data_dir=tmp_path,
        gateway_host="127.0.0.1",
        gateway_port=7422,
        log_level="WARNING",
        log_format="console",
        backend_timeout_seconds=5.0,
        otlp_enabled=True,
        otlp_endpoint="http://localhost:4318",
        otlp_batch_size=4,
        otlp_flush_interval_seconds=0.05,
    )
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": "Bearer test-token"},
    ) as client, app.router.lifespan_context(app):
        # Replace the live exporter on the wired emitter with a fake.
        emitter: OTLPEmitter = app.state.otlp
        # Restart with the fake exporter; the original processor still exists
        # but we just want a deterministic backing store for the test.
        await emitter.stop()
        emitter.enabled = True
        emitter._events_emitted = 0
        emitter._flush_errors = 0
        emitter._started = False
        await emitter.start(exporter=_FakeExporter())

        emitter.emit(_audit_event_dict())
        emitter.emit(_audit_event_dict(error="boom"))

        r = await client.get("/v1/observability/status")
        assert r.status_code == 200
        body = r.json()
        assert body["otlp_enabled"] is True
        assert body["otlp_endpoint"] == "http://localhost:4318"
        assert body["events_emitted"] == 2
        assert body["last_emit_at"] is not None


@pytest.mark.asyncio
async def test_flush_endpoint_returns_counts(otlp_client) -> None:
    _, client = otlp_client
    r = await client.post("/v1/observability/flush")
    assert r.status_code == 200
    body = r.json()
    # flushed=True even when disabled (nothing to flush counts as success).
    assert body["flushed"] is True
    assert body["events_emitted"] == 0
    assert body["flush_errors"] == 0


@pytest.mark.asyncio
async def test_flush_endpoint_requires_admin_in_strict_mode(tmp_path) -> None:
    """When auth_mode=verify_local without admin scope → 401 on /flush."""
    settings = Settings(
        data_dir=tmp_path,
        gateway_host="127.0.0.1",
        gateway_port=7422,
        log_level="WARNING",
        log_format="console",
        backend_timeout_seconds=5.0,
        otlp_enabled=False,
        auth_mode="verify_local",
        identity_jwt_secret="test-secret-32-bytes-padding-here",
        inbound_auth_required=False,  # bypass shared bearer; rely on JWT
    )

    app = create_app(settings)
    transport = ASGITransport(app=app)

    # Forge a token without admin scope.
    import jwt as pyjwt

    now = int(datetime.now(timezone.utc).timestamp())
    non_admin = pyjwt.encode(
        {
            "sub": "agent-1",
            "iss": "test",
            "aud": settings.jwt_audience,
            "iat": now,
            "exp": now + 3600,
            "jti": "jti-test",
            "agent_id": "agent-1",
            "tenant_id": "default",
            "scopes": ["tool:web.fetch:read"],
        },
        settings.identity_jwt_secret,
        algorithm="HS256",
    )

    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {non_admin}"},
    ) as client, app.router.lifespan_context(app):
        r = await client.post("/v1/observability/flush")
        assert r.status_code == 401
        body = r.json()
        assert body["error"]["code"] == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_flush_endpoint_admin_scope_allowed(tmp_path) -> None:
    """A token with ``tenant:*:admin`` may flush the buffer."""
    settings = Settings(
        data_dir=tmp_path,
        gateway_host="127.0.0.1",
        gateway_port=7422,
        log_level="WARNING",
        log_format="console",
        backend_timeout_seconds=5.0,
        otlp_enabled=False,
        auth_mode="verify_local",
        identity_jwt_secret="test-secret-32-bytes-padding-here",
        inbound_auth_required=False,
    )

    app = create_app(settings)
    transport = ASGITransport(app=app)

    import jwt as pyjwt

    now = int(datetime.now(timezone.utc).timestamp())
    admin = pyjwt.encode(
        {
            "sub": "admin",
            "iss": "test",
            "aud": settings.jwt_audience,
            "iat": now,
            "exp": now + 3600,
            "jti": "jti-admin",
            "agent_id": "admin",
            "tenant_id": "default",
            "scopes": ["tenant:*:admin"],
        },
        settings.identity_jwt_secret,
        algorithm="HS256",
    )

    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {admin}"},
    ) as client, app.router.lifespan_context(app):
        r = await client.post("/v1/observability/flush")
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Full integration — invoke pipeline emits OTLP records end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invoke_emits_otlp_log_record(tmp_path) -> None:
    """A successful tool invoke writes both the audit row AND an OTLP log."""
    import respx

    settings = Settings(
        data_dir=tmp_path,
        gateway_host="127.0.0.1",
        gateway_port=7422,
        log_level="WARNING",
        log_format="console",
        backend_timeout_seconds=5.0,
        otlp_enabled=True,
        otlp_endpoint="http://localhost:4318",
        otlp_batch_size=2,
        otlp_flush_interval_seconds=0.05,
    )

    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": "Bearer test-token"},
    ) as client, app.router.lifespan_context(app):
        emitter: OTLPEmitter = app.state.otlp
        # Hot-swap the exporter so the test owns the buffer.
        await emitter.stop()
        emitter.enabled = True
        emitter._events_emitted = 0
        emitter._flush_errors = 0
        emitter._started = False
        fake = _FakeExporter()
        await emitter.start(exporter=fake)
        # Re-attach to AuditLog (audit was constructed with the original
        # emitter handle, which still routes to ``app.state.otlp``).
        app.state.audit._otlp = emitter

        # Register a tool + mock the backend.
        tool_payload = {
            "tool_id": "web.fetch",
            "name": "Web Fetch",
            "description": "fetch a URL",
            "transport": "http",
            "endpoint": "http://mcp.test/invoke/fetch",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "idempotent": True,
            "side_effects": "read",
            "cache_ttl_seconds": 0,  # disable cache so we always hit backend
            "auth_method": "none",
            "auth_config": {},
        }
        r = await client.post("/v1/tools/register", json=tool_payload)
        assert r.status_code == 201, r.text

        with respx.mock(assert_all_called=False) as router:
            router.post("http://mcp.test/invoke/fetch").mock(
                return_value=httpx.Response(200, json={"content": "ok"})
            )
            r = await client.post(
                "/v1/invoke",
                json={
                    "tool_id": "web.fetch",
                    "arguments": {"url": "mock://x"},
                    "agent_id": "test-agent",
                },
            )
            assert r.status_code == 200, r.text

        await emitter.flush()
        # The exporter must have at least one record from the invoke.
        assert emitter._events_emitted >= 1
        assert len(fake.records) >= 1


@pytest.mark.asyncio
async def test_status_endpoint_unauthenticated_when_inbound_optional(
    tmp_path,
) -> None:
    """When ``inbound_auth_required=False`` the status endpoint is open."""
    settings = Settings(
        data_dir=tmp_path,
        gateway_host="127.0.0.1",
        gateway_port=7422,
        log_level="WARNING",
        log_format="console",
        backend_timeout_seconds=5.0,
        otlp_enabled=False,
        inbound_auth_required=False,
    )
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client, app.router.lifespan_context(app):
        r = await client.get("/v1/observability/status")
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_status_endpoint_requires_bearer_when_configured(tmp_path) -> None:
    """When inbound auth is required, missing bearer → 401."""
    settings = Settings(
        data_dir=tmp_path,
        gateway_host="127.0.0.1",
        gateway_port=7422,
        log_level="WARNING",
        log_format="console",
        backend_timeout_seconds=5.0,
        otlp_enabled=False,
        inbound_auth_required=True,
    )
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client, app.router.lifespan_context(app):
        r = await client.get("/v1/observability/status")
        assert r.status_code == 401
