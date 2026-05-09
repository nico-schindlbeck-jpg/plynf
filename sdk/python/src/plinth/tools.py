# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Client for the Plinth tool gateway.

The :class:`ToolGateway` provides an ergonomic surface over the gateway
service: register / list tools, invoke them (with caching and audit),
dry-run, and query the audit log.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from ._http import HTTPClient
from .exceptions import InvalidArguments, ToolNotFound
from .models import (
    AgentLimits,
    AuditEvent,
    ChainVerifyResult,
    DryRunResponse,
    InvokeResponse,
    LimitsStatus,
    Tool,
    ToolRegistration,
)
from .transactions import TransactionBuilder, TransactionsClient

# ---------------------------------------------------------------------------
# ``since=`` parsing — accepts "1h" / "30m" / "7d" / ISO-8601.
# ---------------------------------------------------------------------------


_DURATION_PATTERN = re.compile(
    r"^\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>s|sec|secs|seconds|m|min|mins|"
    r"minutes|h|hr|hrs|hours|d|day|days|w|wk|wks|weeks)\s*$",
    re.IGNORECASE,
)

_UNIT_TO_SECONDS: dict[str, int] = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "hours": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
    "w": 604800,
    "wk": 604800,
    "wks": 604800,
    "weeks": 604800,
}


def _parse_since(value: str | datetime | None) -> str | None:
    """Convert a relative duration or datetime into ISO-8601 UTC.

    Accepted inputs:
        * ``None`` — passthrough.
        * ``datetime`` — converted to UTC ISO-8601.
        * ``"1h"``, ``"30m"``, ``"7d"`` — relative to *now*.
        * ``"2024-01-01T00:00:00Z"`` — passed through after validating.

    Raises:
        ValueError: If the string cannot be parsed.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return _ensure_utc_iso(value)

    text = value.strip()
    match = _DURATION_PATTERN.match(text)
    if match:
        amount = float(match.group("value"))
        unit = match.group("unit").lower()
        seconds = amount * _UNIT_TO_SECONDS[unit]
        ts = datetime.now(tz=timezone.utc) - timedelta(seconds=seconds)  # noqa: UP017
        return _ensure_utc_iso(ts)

    # Otherwise assume an ISO-8601 datetime; let fromisoformat validate.
    try:
        # Python 3.11 fromisoformat accepts trailing ``Z``; older
        # versions need a manual swap. We're 3.11+ but be defensive.
        normalised = text.replace("Z", "+00:00") if text.endswith("Z") else text
        parsed = datetime.fromisoformat(normalised)
    except ValueError as exc:
        raise ValueError(
            f"Invalid 'since' value: {value!r}. Expected a duration like "
            f"'1h', '30m', '7d' or an ISO-8601 timestamp."
        ) from exc
    return _ensure_utc_iso(parsed)


def _ensure_utc_iso(ts: datetime) -> str:
    """Convert ``ts`` to UTC and emit an ISO-8601 string."""
    if ts.tzinfo is None:  # noqa: SIM108  - clearer as if/else here
        ts = ts.replace(tzinfo=timezone.utc)  # noqa: UP017
    else:
        ts = ts.astimezone(timezone.utc)  # noqa: UP017
    return ts.isoformat()


# ---------------------------------------------------------------------------
# Tool gateway client
# ---------------------------------------------------------------------------


class ToolGateway:
    """Client for the Plinth tool gateway service."""

    def __init__(self, http: HTTPClient) -> None:
        self._http = http
        # v0.5 — Workflow Transactions client. The CRUD surface lives at
        # ``client.gateway.transactions``; the fluent ``client.gateway.transaction(...)``
        # factory is on this class as a method.
        self.transactions = TransactionsClient(http)

    # -- transactions (v0.5) ------------------------------------------

    def transaction(
        self,
        *,
        workspace_id: str | None = None,
        agent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TransactionBuilder:
        """Start a new pending transaction and return a fluent builder.

        Each :meth:`TransactionBuilder.add` call records one tool call on
        the server side; :meth:`TransactionBuilder.commit` then runs them in
        order with Saga-style compensation on partial failure.
        """
        return TransactionBuilder(
            self.transactions,
            workspace_id=workspace_id,
            agent_id=agent_id,
            metadata=metadata,
        )

    # -- registration --------------------------------------------------

    def register(self, registration: dict[str, Any] | ToolRegistration) -> Tool:
        """Register a tool with the gateway.

        Accepts either a dict matching :class:`ToolRegistration` or an
        instance of that model.
        """
        if isinstance(registration, ToolRegistration):
            payload = registration.model_dump(mode="json")
        else:
            # Validate up front so the user gets a clean error message
            # before the HTTP roundtrip.
            payload = ToolRegistration.model_validate(registration).model_dump(mode="json")

        response = self._http.post(
            "/v1/tools/register",
            json=payload,
        )
        return Tool.model_validate(response.json())

    def list(self) -> list[Tool]:
        """Return every tool registered with the gateway."""
        data = self._http.get_json("/v1/tools")
        return [Tool.model_validate(t) for t in data.get("tools", [])]

    def get(self, tool_id: str) -> Tool:
        """Fetch a single registered tool by ID."""
        data = self._http.get_json(
            f"/v1/tools/{tool_id}",
            not_found_class=ToolNotFound,
        )
        return Tool.model_validate(data)

    def deregister(self, tool_id: str) -> None:
        """Remove a tool from the gateway."""
        self._http.delete(
            f"/v1/tools/{tool_id}",
            not_found_class=ToolNotFound,
        )

    # -- invoke --------------------------------------------------------

    def invoke(
        self,
        tool_id: str,
        arguments: dict[str, Any] | None = None,
        *,
        workspace_id: str | None = None,
        agent_id: str | None = None,
        cache: bool = True,
        idempotency_key: str | None = None,
    ) -> InvokeResponse:
        """Invoke a tool through the gateway.

        Args:
            tool_id: The tool identifier (e.g. ``"web.fetch"``).
            arguments: Tool-specific arguments. Defaults to ``{}``.
            workspace_id: For audit attribution.
            agent_id: For audit attribution.
            cache: Whether the gateway may serve a cached response.
            idempotency_key: Optional client-supplied idempotency key.

        Returns:
            An :class:`InvokeResponse` containing the result and the
            audit/cache metadata.
        """
        body: dict[str, Any] = {
            "tool_id": tool_id,
            "arguments": arguments or {},
            "cache": cache,
        }
        if workspace_id is not None:
            body["workspace_id"] = workspace_id
        if agent_id is not None:
            body["agent_id"] = agent_id
        if idempotency_key is not None:
            body["idempotency_key"] = idempotency_key

        response = self._http.post(
            "/v1/invoke",
            json=body,
            not_found_class=ToolNotFound,
        )
        return InvokeResponse.model_validate(response.json())

    def dry_run(
        self,
        tool_id: str,
        arguments: dict[str, Any] | None = None,
        *,
        workspace_id: str | None = None,
        agent_id: str | None = None,
    ) -> DryRunResponse:
        """Predict whether ``invoke`` would hit cache and what it costs.

        Useful for budget guards before paying for a real invocation.
        """
        body: dict[str, Any] = {
            "tool_id": tool_id,
            "arguments": arguments or {},
        }
        if workspace_id is not None:
            body["workspace_id"] = workspace_id
        if agent_id is not None:
            body["agent_id"] = agent_id

        response = self._http.post(
            "/v1/invoke/dry-run",
            json=body,
            not_found_class=ToolNotFound,
        )
        return DryRunResponse.model_validate(response.json())

    # -- audit / stats -------------------------------------------------

    def audit(
        self,
        *,
        workspace_id: str | None = None,
        tool_id: str | None = None,
        since: str | datetime | None = None,
        limit: int | None = None,
    ) -> list[AuditEvent]:
        """Query the gateway's audit log.

        Args:
            workspace_id: Restrict to a single workspace.
            tool_id: Restrict to a single tool.
            since: Either a relative duration (``"1h"``, ``"30m"``,
                ``"7d"``) or an ISO-8601 timestamp.
            limit: Maximum number of events to return.
        """
        try:
            since_iso = _parse_since(since)
        except ValueError as exc:
            raise InvalidArguments(str(exc)) from exc

        params: dict[str, Any] = {
            "workspace_id": workspace_id,
            "tool_id": tool_id,
            "since": since_iso,
            "limit": limit,
        }
        data = self._http.get_json("/v1/audit", params=params)
        return [AuditEvent.model_validate(e) for e in data.get("events", [])]

    def stats(
        self,
        *,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        """Return aggregated audit statistics."""
        params = {"workspace_id": workspace_id}
        data = self._http.get_json("/v1/audit/stats", params=params)
        # Some servers wrap the payload in {"stats": {...}}, others
        # return it flat. Normalise so callers always see the inner dict.
        if isinstance(data, dict) and "stats" in data and isinstance(data["stats"], dict):
            return data["stats"]
        return data

    def verify_audit_chain(
        self,
        *,
        since: str | datetime | None = None,
        limit: int = 1000,
    ) -> ChainVerifyResult:
        """Verify the gateway audit hash chain (v1.0 tamper-evidence).

        ``since`` accepts the same shapes as :meth:`audit` (relative
        duration, datetime, or ISO-8601). The server walks events
        forward from that cutoff and reports the first hash mismatch.
        """

        params: dict[str, Any] = {"limit": int(limit)}
        if since is not None:
            try:
                iso = _parse_since(since)
            except ValueError as exc:
                raise InvalidArguments(str(exc)) from exc
            if iso is not None:
                # Server expects a unix timestamp.
                ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
                params["since"] = int(ts.timestamp())
        data = self._http.get_json("/v1/audit/verify", params=params)
        return ChainVerifyResult.model_validate(data)

    # -- cache ---------------------------------------------------------

    def cache_stats(self) -> dict[str, Any]:
        """Return ``{"hits", "misses", "size_bytes"}``-style cache info."""
        return self._http.get_json("/v1/cache/stats")

    def clear_cache(self, *, tool_id: str | None = None) -> None:
        """Clear the gateway cache (optionally scoped to one tool)."""
        params = {"tool_id": tool_id}
        self._http.delete("/v1/cache", params=params)

    # -- per-agent rate / cost limits (v0.2) ---------------------------

    def set_limits(
        self,
        agent_id: str,
        *,
        rpm: int | None = None,
        burst: int | None = None,
        cost_cap_usd_hour: float | None = None,
        cost_cap_usd_day: float | None = None,
    ) -> AgentLimits:
        """Override the rate-limit / cost-cap configuration for an agent.

        Any field left as ``None`` is omitted from the payload, so the
        server falls back to its default for that knob.

        Args:
            agent_id: The agent to configure.
            rpm: Requests-per-minute cap.
            burst: Burst allowance on top of ``rpm``.
            cost_cap_usd_hour: Rolling-hour cost ceiling (USD).
            cost_cap_usd_day: Rolling-day cost ceiling (USD).
        """
        body: dict[str, Any] = {"agent_id": agent_id}
        if rpm is not None:
            body["rpm"] = rpm
        if burst is not None:
            body["burst"] = burst
        if cost_cap_usd_hour is not None:
            body["cost_cap_usd_hour"] = cost_cap_usd_hour
        if cost_cap_usd_day is not None:
            body["cost_cap_usd_day"] = cost_cap_usd_day

        response = self._http.post(
            f"/v1/limits/{agent_id}",
            json=body,
        )
        return AgentLimits.model_validate(response.json())

    def get_limits(self, agent_id: str) -> AgentLimits:
        """Return the current per-agent limits configuration."""
        data = self._http.get_json(f"/v1/limits/{agent_id}")
        return AgentLimits.model_validate(data)

    def get_limits_status(self, agent_id: str) -> LimitsStatus:
        """Return the agent's current usage against its configured limits."""
        data = self._http.get_json(f"/v1/limits/{agent_id}/status")
        return LimitsStatus.model_validate(data)

    def reset_limits(self, agent_id: str) -> None:
        """Delete the per-agent override (revert to gateway defaults)."""
        self._http.delete(f"/v1/limits/{agent_id}")


__all__ = ["ToolGateway"]
