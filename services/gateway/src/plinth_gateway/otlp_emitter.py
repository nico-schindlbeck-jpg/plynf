# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""OTLP/HTTP log emitter for gateway audit events.

When ``PLINTH_OTLP_ENABLED=true`` the gateway forwards every recorded audit
event to an OpenTelemetry collector as an OTel ``LogRecord``. This is *purely
additive* — the existing ``audit_events`` SQLite table is still written, and
emission is best-effort: any failure here is logged + counted but never
crashes a tool invocation.

Design notes
------------
* The emitter is constructed eagerly at startup but only initializes the OTel
  ``LoggerProvider`` when ``enabled=True``. When disabled, :meth:`emit` is a
  no-op and the status endpoint reports ``otlp_enabled=False``.
* The OTel SDK ships its own ``BatchLogRecordProcessor`` which handles batching
  and background flushing. We wrap it so callers see a single, simple
  interface (``start`` / ``emit`` / ``flush`` / ``stop``).
* For testability we accept an *optional* ``exporter`` argument — production
  hands in an :class:`OTLPLogExporter`, tests can pass an in-memory exporter
  to verify the encoded events without spinning up a collector.
* Every dict written to ``attributes`` must be flat → primitive (OTel's
  attribute typing). :func:`_flatten_attributes` walks nested dicts with dot
  notation and JSON-encodes lists; binary blobs are skipped.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Optional, Protocol  # noqa: UP035

import structlog


_log = structlog.get_logger(__name__)


# Lazy imports — the OTel SDK is only loaded when ``enabled=True`` so the
# disabled path stays import-light and v0.3 behaviour is unchanged.
#
# v1.1 — public ``logs`` API migration. Upstream OTel kept the public path
# under ``opentelemetry.sdk._logs`` even after the 1.30 cut (see
# python-contrib repo issues #4045/#4055 — "Logs SDK is now stable but the
# top-level public path was not added"). To stay forward-compatible and
# unblock the ``<1.30`` pin lift we use a try-import pattern: the public
# name wins when present, the underscore name is the fallback. See
# CONTRACTS.md v1.1 — "OTel — public ``logs`` API".


class _ExporterLike(Protocol):
    """Minimal export protocol — we only call ``export`` + ``shutdown``."""

    def export(self, batch: Any) -> Any: ...  # pragma: no cover - protocol
    def shutdown(self) -> None: ...  # pragma: no cover - protocol


_PREVIEW_LIMIT = 500


def _import_otel_logs() -> tuple[Any, Any, Any]:
    """Return ``(LoggerProvider, BatchLogRecordProcessor, LogRecord)``.

    Tries the public ``opentelemetry.sdk.logs`` namespace first (target end
    state once upstream promotes the module) and falls back to the legacy
    ``opentelemetry.sdk._logs`` underscore path which remains the canonical
    location through at least 1.41. Importers should call this helper in
    place of writing the import directly so the migration stays in one
    spot.
    """

    # Try four candidate locations in order of preference:
    #   1. public namespace, both names in __init__         (future-canonical)
    #   2. public namespace, LogRecord in _internal         (some 1.30+ versions)
    #   3. legacy underscore, both names in __init__        (1.25-1.29 canonical)
    #   4. legacy underscore, LogRecord in _internal        (1.30+ where LogRecord moved)
    LoggerProvider = None
    BatchLogRecordProcessor = None
    LogRecord = None
    last_exc: ImportError | None = None
    for logs_mod, internal_mod, export_mod in [
        ("opentelemetry.sdk.logs", "opentelemetry.sdk.logs._internal", "opentelemetry.sdk.logs.export"),
        ("opentelemetry.sdk._logs", "opentelemetry.sdk._logs._internal", "opentelemetry.sdk._logs.export"),
    ]:
        try:
            import importlib
            logs = importlib.import_module(logs_mod)
            export = importlib.import_module(export_mod)
            LoggerProvider = getattr(logs, "LoggerProvider")
            BatchLogRecordProcessor = getattr(export, "BatchLogRecordProcessor")
            # LogRecord may live in module __init__ or in _internal depending on version
            try:
                LogRecord = getattr(logs, "LogRecord")
            except AttributeError:
                internal = importlib.import_module(internal_mod)
                LogRecord = getattr(internal, "LogRecord")
            return LoggerProvider, BatchLogRecordProcessor, LogRecord
        except (ImportError, AttributeError) as e:
            last_exc = ImportError(str(e))
            continue
    raise last_exc or ImportError("opentelemetry.sdk logs module not found in any known location")


def _flatten_attributes(
    event: dict[str, Any],
    *,
    prefix: str = "",
    out: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Walk ``event`` into flat ``key→primitive`` pairs OTel can attribute.

    OTel attribute values must be one of: ``str | bool | int | float`` or a
    homogeneous sequence thereof. Anything else (nested dict, list of dicts,
    bytes) is JSON-stringified so the value still flows through; structured
    consumers can ``json.loads`` on the receive side.
    """
    if out is None:
        out = {}
    for key, value in event.items():
        full = f"{prefix}{key}" if not prefix else f"{prefix}.{key}"
        if value is None:
            continue
        if isinstance(value, bool):
            out[full] = value
        elif isinstance(value, (int, float)):
            out[full] = value
        elif isinstance(value, str):
            out[full] = value
        elif isinstance(value, dict):
            _flatten_attributes(value, prefix=full, out=out)
        elif isinstance(value, (list, tuple)):
            # Homogeneous primitive lists pass through; mixed/nested → JSON.
            if value and all(isinstance(v, (str, int, float, bool)) for v in value):
                out[full] = list(value)
            else:
                try:
                    out[full] = json.dumps(value, default=str)
                except (TypeError, ValueError):
                    out[full] = str(value)
        else:
            # datetimes / arbitrary objects → str fallback.
            out[full] = str(value)
    return out


def _build_attributes(
    event: dict[str, Any],
    service_name: str,
    *,
    service_version: str | None = None,
    region_id: str | None = None,
) -> dict[str, Any]:
    """Map an audit event dict to the canonical OTel attribute namespace.

    The mapping mirrors the contract in ``docs/observability.md`` v1.0:

    * Common (every event)::

          service.name = "plinth-gateway"
          service.version = "1.0.0"
          region.id = "..."   (only when configured)

    * Per-tenant scope::  ``tenant.id``, ``agent.id``
    * Per-workflow scope:: ``workflow.id``, ``workflow.step``
    * Per-tool scope::    ``tool.id``, ``tool.cached``,
                          ``tool.duration_ms``, ``tool.cost_usd``
    * Per-workspace scope:: ``workspace.id``

    Top-level fields like ``tool_id`` are mapped to dotted attributes
    (``tool.id``); ``cost_estimate_usd`` becomes ``tool.cost_usd``. Any
    field already in dotted form (``actor.kind``) flows through
    ``_flatten_attributes`` unchanged.

    The audit-event input may use either snake_case (``workflow_id``) or
    dotted (``workflow.id``) field names; both forms are accepted.
    """
    attrs: dict[str, Any] = {"service.name": service_name}
    if service_version:
        attrs["service.version"] = str(service_version)
    if region_id:
        attrs["region.id"] = str(region_id)

    # Per-tool scope.
    if event.get("tool_id") is not None:
        attrs["tool.id"] = event["tool_id"]
    if event.get("cached") is not None:
        attrs["tool.cached"] = bool(event["cached"])
    if event.get("duration_ms") is not None:
        attrs["tool.duration_ms"] = int(event["duration_ms"])
    if event.get("cost_estimate_usd") is not None:
        attrs["tool.cost_usd"] = float(event["cost_estimate_usd"])

    # Per-tenant + agent scope.
    if event.get("agent_id"):
        attrs["agent.id"] = str(event["agent_id"])
    if event.get("tenant_id"):
        attrs["tenant.id"] = str(event["tenant_id"])

    # Per-workspace scope.
    if event.get("workspace_id"):
        attrs["workspace.id"] = str(event["workspace_id"])

    # Per-workflow scope. Accept either snake_case (``workflow_id``,
    # ``workflow_step``) or dotted (``workflow.id``, ``workflow.step``)
    # input — operators sending events from custom emitters use both
    # in the wild.
    workflow_id = event.get("workflow_id") or event.get("workflow.id")
    workflow_step = event.get("workflow_step") or event.get("workflow.step")
    if workflow_id:
        attrs["workflow.id"] = str(workflow_id)
    if workflow_step:
        attrs["workflow.step"] = str(workflow_step)

    if event.get("arguments_hash"):
        attrs["arguments.hash"] = str(event["arguments_hash"])
    if event.get("result_hash"):
        attrs["result.hash"] = str(event["result_hash"])
    if event.get("arguments_preview"):
        # Truncate defensively even though make_preview already enforces 500.
        attrs["arguments.preview"] = str(event["arguments_preview"])[:_PREVIEW_LIMIT]

    if event.get("error"):
        attrs["error.message"] = str(event["error"])

    if event.get("id"):
        attrs["audit.id"] = str(event["id"])

    # Anything we haven't claimed yet — flatten under its own namespace so
    # operators can introspect arbitrary extensions.
    extras = {
        k: v
        for k, v in event.items()
        if k
        not in {
            "id",
            "timestamp",
            "tool_id",
            "cached",
            "duration_ms",
            "cost_estimate_usd",
            "agent_id",
            "tenant_id",
            "workspace_id",
            "workflow_id",
            "workflow.id",
            "workflow_step",
            "workflow.step",
            "arguments_hash",
            "arguments_preview",
            "result_hash",
            "error",
            "arguments",  # raw args contain tool inputs; preview is enough
        }
        and v is not None
    }
    if extras:
        _flatten_attributes(extras, prefix="extra", out=attrs)
    return attrs


def _event_severity(event: dict[str, Any]) -> tuple[Any, str]:
    """Return ``(severity_number, severity_text)`` for the OTel record.

    Failed invocations → ERROR. Otherwise INFO.
    """
    from opentelemetry._logs import SeverityNumber

    if event.get("error"):
        return SeverityNumber.ERROR, "ERROR"
    return SeverityNumber.INFO, "INFO"


def _parse_event_timestamp(event: dict[str, Any]) -> int:
    """Return the OTel timestamp_unix_nano for ``event``.

    Falls back to ``time.time_ns()`` when the event timestamp is missing or
    cannot be parsed — we never want to drop an event because of a clock
    issue.
    """
    raw = event.get("timestamp")
    if raw is None:
        return time.time_ns()
    if isinstance(raw, datetime):
        ts = raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
        return int(ts.timestamp() * 1_000_000_000)
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return int(ts.timestamp() * 1_000_000_000)
    except (ValueError, TypeError):
        return time.time_ns()


class OTLPEmitter:
    """Buffered OTLP/HTTP log emitter for gateway audit events.

    The emitter is safe to construct unconditionally: when
    ``settings.otlp_enabled=False`` the constructor is the only thing that
    runs and :meth:`emit` becomes a no-op.

    Production lifecycle::

        emitter = OTLPEmitter(settings)
        await emitter.start()              # initialize provider+exporter
        # ... event loop ...
        emitter.emit(audit_event_dict)     # called from AuditLog.record
        await emitter.flush()              # /v1/observability/flush
        await emitter.stop()               # on shutdown

    For tests that don't want a real OTLPLogExporter, pass ``exporter=`` into
    :meth:`start` or use :class:`InMemoryLogExporter`.
    """

    def __init__(self, settings: Any) -> None:
        self.enabled: bool = bool(getattr(settings, "otlp_enabled", False))
        self.endpoint: str = str(getattr(settings, "otlp_endpoint", ""))
        self.service_name: str = str(
            getattr(settings, "otlp_service_name", "plinth-gateway")
        )
        # v1.0 — propagate ``service.version`` + ``region.id`` so every
        # emitted log carries the canonical attribute set documented in
        # ``docs/observability.md``. ``service_version`` defaults to the
        # gateway's pyproject ``__version__`` when settings doesn't set
        # it explicitly.
        try:
            from plinth_gateway import __version__ as _pkg_version

            default_version: str | None = str(_pkg_version)
        except Exception:  # noqa: BLE001 — best-effort
            default_version = None
        self.service_version: str | None = (
            getattr(settings, "otlp_service_version", None)
            or default_version
        )
        self.region_id: str | None = (
            getattr(settings, "region_id", None) or None
        )
        self.batch_size: int = int(getattr(settings, "otlp_batch_size", 64))
        self.flush_interval: float = float(
            getattr(settings, "otlp_flush_interval_seconds", 2.0)
        )

        raw_headers = getattr(settings, "otlp_headers_json", "") or "{}"
        try:
            parsed = json.loads(raw_headers)
            self.headers: dict[str, str] = (
                {str(k): str(v) for k, v in parsed.items()}
                if isinstance(parsed, dict)
                else {}
            )
        except (TypeError, ValueError):
            _log.warning(
                "otlp.headers_invalid",
                raw=raw_headers,
                hint="PLINTH_OTLP_HEADERS_JSON must be a JSON object",
            )
            self.headers = {}

        self._events_emitted: int = 0
        self._flush_errors: int = 0
        self._last_emit_at: Optional[datetime] = None  # noqa: UP007
        self._provider: Any | None = None
        self._processor: Any | None = None
        self._logger: Any | None = None
        self._started: bool = False

    # ------------------------------------------------------------------ lifecycle

    async def start(self, exporter: _ExporterLike | None = None) -> None:
        """Initialize the OTel ``LoggerProvider`` (or stay a no-op).

        ``exporter`` is exposed for tests — production passes ``None`` and the
        emitter constructs an :class:`OTLPLogExporter` aimed at
        ``settings.otlp_endpoint``.
        """
        if not self.enabled or self._started:
            self._started = True
            return

        try:
            from opentelemetry.sdk.resources import Resource

            LoggerProvider, BatchLogRecordProcessor, _ = _import_otel_logs()
            resource = Resource.create({"service.name": self.service_name})
            self._provider = LoggerProvider(resource=resource)

            if exporter is None:
                from opentelemetry.exporter.otlp.proto.http._log_exporter import (
                    OTLPLogExporter,
                )

                exporter = OTLPLogExporter(
                    endpoint=f"{self.endpoint.rstrip('/')}/v1/logs",
                    headers=self.headers or None,
                )

            self._processor = BatchLogRecordProcessor(
                exporter,
                max_export_batch_size=self.batch_size,
                schedule_delay_millis=int(self.flush_interval * 1000),
            )
            self._provider.add_log_record_processor(self._processor)
            self._logger = self._provider.get_logger("plinth-gateway")
            self._started = True
            _log.info(
                "otlp.started",
                endpoint=self.endpoint,
                service_name=self.service_name,
                batch_size=self.batch_size,
                flush_interval=self.flush_interval,
            )
        except Exception as exc:  # noqa: BLE001 - never let OTel break startup
            self._flush_errors += 1
            self._started = True
            self.enabled = False  # disable the emit path so we don't keep failing
            _log.warning("otlp.start_failed", error=str(exc))

    async def flush(self) -> bool:
        """Force-flush the underlying batch processor.

        Returns True if the flush completed cleanly (or there was nothing to
        flush because OTLP is disabled), False if the underlying processor
        reported failure.
        """
        if not self.enabled or self._processor is None:
            return True
        try:
            timeout_ms = int(max(self.flush_interval, 1.0) * 1000)
            ok = self._processor.force_flush(timeout_ms)
            if not ok:
                self._flush_errors += 1
            return bool(ok)
        except Exception as exc:  # noqa: BLE001 - flush must never crash callers
            self._flush_errors += 1
            _log.warning("otlp.flush_failed", error=str(exc))
            return False

    async def stop(self) -> None:
        """Flush + shut down the provider."""
        if not self.enabled or self._provider is None:
            self._started = False
            return
        try:
            await self.flush()
            self._provider.shutdown()
        except Exception as exc:  # noqa: BLE001 - shutdown must never crash
            _log.warning("otlp.stop_failed", error=str(exc))
        finally:
            self._started = False
            self._provider = None
            self._processor = None
            self._logger = None

    # ------------------------------------------------------------------ emit

    def emit(self, event: dict[str, Any]) -> None:
        """Enqueue ``event`` for OTLP emission.

        Non-blocking + best-effort. Any exception is swallowed and counted on
        ``flush_errors`` so the audit pipeline keeps working when the
        collector is down or misconfigured.
        """
        if not self.enabled or self._logger is None:
            return
        try:
            _, _, LogRecord = _import_otel_logs()

            severity_number, severity_text = _event_severity(event)
            # Provide explicit zero trace/span IDs + trace_flags — without
            # these the OTLP proto encoder crashes inside the batch processor
            # when a record has no active span context (the encoder calls
            # ``span_id.to_bytes(...)`` on None and ``int(trace_flags)`` on
            # None). The contract treats these as "no parent trace" → zero.
            from opentelemetry.trace import TraceFlags

            record = LogRecord(
                timestamp=_parse_event_timestamp(event),
                trace_id=0,
                span_id=0,
                trace_flags=TraceFlags(0),
                severity_number=severity_number,
                severity_text=severity_text,
                body=str(event.get("type") or "tool.invoked"),
                attributes=_build_attributes(
                    event,
                    self.service_name,
                    service_version=self.service_version,
                    region_id=self.region_id,
                ),
            )
            self._logger.emit(record)
            self._events_emitted += 1
            self._last_emit_at = datetime.now(timezone.utc)
        except Exception as exc:  # noqa: BLE001 - emit must never crash
            self._flush_errors += 1
            _log.warning("otlp.emit_failed", error=str(exc))

    # ------------------------------------------------------------------ status

    @property
    def status(self) -> dict[str, Any]:
        """Return a JSON-serialisable status snapshot.

        Shape matches ``GET /v1/observability/status`` in CONTRACTS.md v0.4.
        ``otlp_endpoint`` is None when disabled so the dashboard can hide the
        endpoint from the UI cleanly.
        """
        return {
            "otlp_enabled": self.enabled,
            "otlp_endpoint": self.endpoint if self.enabled else None,
            "otlp_service_name": self.service_name,
            "events_emitted": self._events_emitted,
            "last_emit_at": (
                self._last_emit_at.isoformat() if self._last_emit_at else None
            ),
            "flush_errors": self._flush_errors,
        }


__all__ = [
    "OTLPEmitter",
    "_build_attributes",
    "_flatten_attributes",
    "_event_severity",
    "_import_otel_logs",
    "_parse_event_timestamp",
]
