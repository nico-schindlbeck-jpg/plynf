# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Tier-gate middleware — Free / Pro / Enterprise enforcement.

The gate runs *after* authentication has resolved a tenant_id, and decides
whether the call is allowed under the tenant's tier. It is the single
enforcement point for the freemium model.

Gate axes (in priority order):

  1. **Monthly token budget**: each tier caps shaped-tokens-per-month. Free
     stops at 100k; Pro at 5M; Enterprise unlimited.
  2. **Connector count**: Free can only use 3 connectors. Pro all 8. Enterprise
     plus custom REST imports.
  3. **Tenant count**: Free 1, Pro 10, Enterprise unlimited (this is
     enforced at tenant-create time, not per-request — included for
     completeness).
  4. **Feature flags**: PII redaction, audit-log retention, Postgres-backed
     savings, SSO — all gated by tier.

The integration *type* (proxy / MCP / SDK / webhook) is **not** gated.
Customer feedback was unanimous that gating on integration kills adoption.
Gating on volume + features is the standard SaaS pattern and is what we ship.

Counter usage is in-memory for MVP — production swaps in Redis. Reset is
calendar-month boundaries, computed in UTC.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Literal

Tier = Literal["free", "pro", "enterprise"]


@dataclass(frozen=True)
class TierLimits:
    """Hard limits per tier."""

    tier: Tier
    monthly_token_budget: int | None  # None = unlimited
    max_connectors: int | None
    max_tenants: int | None
    allow_pii_redaction: bool
    allow_audit_log: bool
    allow_self_hosted_helm: bool
    allow_custom_rest_connectors: bool
    audit_log_retention_days: int  # 0 = none


# The actual tier matrix. Free is intentionally generous on integration types
# (any: proxy / MCP / SDK / n8n / webhook all work) so devs can fully evaluate
# Plynf without paying. The squeeze is volume + features.
TIERS: dict[Tier, TierLimits] = {
    "free": TierLimits(
        tier="free",
        monthly_token_budget=100_000,
        max_connectors=3,
        max_tenants=1,
        allow_pii_redaction=False,
        allow_audit_log=False,
        allow_self_hosted_helm=False,
        allow_custom_rest_connectors=False,
        audit_log_retention_days=0,
    ),
    "pro": TierLimits(
        tier="pro",
        monthly_token_budget=5_000_000,
        max_connectors=None,  # all 8 shipped
        max_tenants=10,
        allow_pii_redaction=True,
        allow_audit_log=True,
        allow_self_hosted_helm=False,
        allow_custom_rest_connectors=True,
        audit_log_retention_days=90,
    ),
    "enterprise": TierLimits(
        tier="enterprise",
        monthly_token_budget=None,
        max_connectors=None,
        max_tenants=None,
        allow_pii_redaction=True,
        allow_audit_log=True,
        allow_self_hosted_helm=True,
        allow_custom_rest_connectors=True,
        audit_log_retention_days=365 * 7,
    ),
}


@dataclass
class _Usage:
    tokens_this_month: int = 0
    month_key: str = ""  # "YYYY-MM" in UTC


@dataclass
class TierGate:
    """Counts usage per tenant and decides whether to allow a call.

    The MVP uses an in-memory dict guarded by a single lock. Production
    deployments inject a Redis-backed implementation that respects the same
    interface (``record_tokens``, ``check``).
    """

    _usage: dict[str, _Usage] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)

    @staticmethod
    def _month_key(now: float | None = None) -> str:
        ts = now if now is not None else time.time()
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return f"{dt.year:04d}-{dt.month:02d}"

    def record_tokens(self, tenant_id: str, tokens: int) -> None:
        """Add ``tokens`` to this tenant's monthly counter."""
        key = self._month_key()
        with self._lock:
            usage = self._usage.get(tenant_id)
            if usage is None or usage.month_key != key:
                usage = _Usage(tokens_this_month=0, month_key=key)
                self._usage[tenant_id] = usage
            usage.tokens_this_month += max(0, tokens)

    def usage(self, tenant_id: str) -> int:
        """Current month's used tokens for tenant."""
        key = self._month_key()
        with self._lock:
            usage = self._usage.get(tenant_id)
            if usage is None or usage.month_key != key:
                return 0
            return usage.tokens_this_month

    def all_usage(self) -> dict[str, int]:
        """Current month's used tokens for every known tenant.

        Tenants whose last recorded usage fell in a previous month are omitted
        (their counter has effectively reset to 0). Powers the ``/metrics``
        exporter's per-tenant gauges without reaching into private state.
        """
        key = self._month_key()
        with self._lock:
            return {
                tid: u.tokens_this_month
                for tid, u in self._usage.items()
                if u.month_key == key
            }

    def check(
        self,
        tenant_id: str,
        tier: Tier,
        *,
        connector_count: int | None = None,
    ) -> tuple[bool, str | None]:
        """Return ``(allowed, reason_if_blocked)``.

        ``reason_if_blocked`` is a short machine-readable string (used as
        the body of the 402/429 response) — never raw tenant state.
        """
        limits = TIERS.get(tier)
        if limits is None:
            return False, f"unknown_tier:{tier}"

        # Volume cap.
        if limits.monthly_token_budget is not None:
            used = self.usage(tenant_id)
            if used >= limits.monthly_token_budget:
                return False, "monthly_token_budget_exceeded"

        # Connector-count cap (informational; the API layer enforces during
        # connector-add operations, not per-call).
        if (
            connector_count is not None
            and limits.max_connectors is not None
            and connector_count > limits.max_connectors
        ):
            return False, "connector_count_exceeded"

        return True, None


def upgrade_hint(current: Tier) -> str:
    """User-facing copy returned with 402/429 responses."""
    if current == "free":
        return (
            "Free tier capped at 100,000 shaped tokens / month and 3 connectors. "
            "Upgrade to Pro for 5M tokens, all 8 connectors, PII redaction, "
            "and 90-day audit log retention."
        )
    if current == "pro":
        return (
            "Pro tier capped at 5M shaped tokens / month. For unlimited volume, "
            "self-hosted deployment, SSO, and dedicated support, contact "
            "sales@plynf.com about Enterprise."
        )
    return "Enterprise has no published cap — please reach out to your account team."


__all__ = [
    "TIERS",
    "Tier",
    "TierGate",
    "TierLimits",
    "upgrade_hint",
]
