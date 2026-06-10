# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Stripe subscription billing — Checkout, Customer Portal, webhooks.

Talks to the Stripe REST API directly over httpx (form-encoded; no SDK
dependency). Three pieces:

* :class:`StripeClient` — create Checkout Sessions (subscription mode) and
  Customer-Portal sessions.
* :func:`verify_webhook_signature` — Stripe's ``t=…,v1=…`` HMAC-SHA256
  scheme, constant-time compare, with a replay-window check.
* :func:`apply_webhook_event` — maps the three lifecycle events we care
  about onto the account store:

  - ``checkout.session.completed``      → activate the purchased plan
  - ``customer.subscription.updated``   → re-map price → plan (up/downgrade)
  - ``customer.subscription.deleted``   → back to free

If no Stripe key is configured the dashboard runs in **simulated billing**
mode: checkout "succeeds" instantly and flips the plan directly. That keeps
the offline demo and tests fully functional — and makes go-live a pure
configuration change (set 4 env vars), not a code change.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import httpx

STRIPE_API = "https://api.stripe.com"

# Default tolerance for webhook timestamp skew (Stripe recommends 5 min).
WEBHOOK_TOLERANCE_SECONDS = 300


class StripeError(RuntimeError):
    """Raised when a Stripe API call fails."""


class StripeClient:
    """Minimal async Stripe client for subscriptions."""

    def __init__(self, secret_key: str, client: httpx.AsyncClient | None = None) -> None:
        self.secret_key = secret_key
        self._client = client

    @property
    def configured(self) -> bool:
        return bool(self.secret_key)

    async def _post(self, path: str, data: dict[str, str]) -> dict[str, Any]:
        own = self._client is None
        client = self._client or httpx.AsyncClient(timeout=20.0)
        try:
            resp = await client.post(
                f"{STRIPE_API}{path}",
                data=data,
                auth=(self.secret_key, ""),
            )
            body = resp.json()
            if resp.status_code >= 400:
                msg = (body.get("error") or {}).get("message", f"HTTP {resp.status_code}")
                raise StripeError(f"stripe: {msg}")
            return body
        finally:
            if own:
                await client.aclose()

    async def create_checkout_session(
        self,
        *,
        price_id: str,
        customer_email: str,
        success_url: str,
        cancel_url: str,
        plan: str,
    ) -> dict[str, Any]:
        """Create a subscription Checkout Session; returns {id, url}."""
        return await self._post(
            "/v1/checkout/sessions",
            {
                "mode": "subscription",
                "line_items[0][price]": price_id,
                "line_items[0][quantity]": "1",
                "customer_email": customer_email,
                "client_reference_id": customer_email,
                "success_url": success_url,
                "cancel_url": cancel_url,
                "metadata[plan]": plan,
                "subscription_data[metadata][plan]": plan,
                "allow_promotion_codes": "true",
            },
        )

    async def create_portal_session(self, *, customer_id: str, return_url: str) -> dict[str, Any]:
        """Create a Customer-Portal session; returns {url}."""
        return await self._post(
            "/v1/billing_portal/sessions",
            {"customer": customer_id, "return_url": return_url},
        )


# ---------------------------------------------------------------------------
# Webhook verification + event application
# ---------------------------------------------------------------------------


def verify_webhook_signature(
    payload: bytes,
    sig_header: str,
    secret: str,
    *,
    tolerance_seconds: int = WEBHOOK_TOLERANCE_SECONDS,
    now: float | None = None,
) -> bool:
    """Validate Stripe's ``Stripe-Signature`` header for ``payload``."""
    if not sig_header or not secret:
        return False
    timestamp = ""
    candidates: list[str] = []
    for part in sig_header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            timestamp = value
        elif key == "v1":
            candidates.append(value)
    if not timestamp or not candidates:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    current = now if now is not None else time.time()
    if abs(current - ts) > tolerance_seconds:
        return False
    signed_payload = f"{timestamp}.".encode() + payload
    expected = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, c) for c in candidates)


def plan_for_price(price_id: str, price_to_plan: dict[str, str]) -> str | None:
    return price_to_plan.get(price_id)


def apply_webhook_event(
    event: dict[str, Any],
    accounts,
    price_to_plan: dict[str, str],
) -> dict[str, Any]:
    """Apply a parsed Stripe event to the account store.

    Returns a small audit dict describing what happened (also used by tests).
    """
    etype = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}

    if etype == "checkout.session.completed":
        email = obj.get("client_reference_id") or (obj.get("customer_details") or {}).get("email")
        plan = (obj.get("metadata") or {}).get("plan")
        if not email or plan not in ("pro", "enterprise"):
            return {"handled": False, "reason": "missing email or plan"}
        accounts.attach_stripe(
            email,
            customer_id=obj.get("customer"),
            subscription_id=obj.get("subscription"),
        )
        updated = accounts.set_plan(email, plan)
        return {"handled": updated is not None, "action": "activate", "email": email, "plan": plan}

    if etype == "customer.subscription.updated":
        customer = obj.get("customer")
        email = accounts.email_for_customer(customer) if customer else None
        if not email:
            return {"handled": False, "reason": "unknown customer"}
        # Subscription cancelled at period end still shows status=active;
        # the deleted event below does the actual downgrade.
        items = ((obj.get("items") or {}).get("data")) or []
        price_id = (items[0].get("price") or {}).get("id") if items else None
        plan = (obj.get("metadata") or {}).get("plan") or (
            plan_for_price(price_id, price_to_plan) if price_id else None
        )
        status = obj.get("status")
        if status in ("canceled", "unpaid", "incomplete_expired"):
            accounts.set_plan(email, "free")
            return {"handled": True, "action": "downgrade", "email": email, "plan": "free"}
        if plan in ("pro", "enterprise"):
            accounts.set_plan(email, plan)
            return {"handled": True, "action": "update", "email": email, "plan": plan}
        return {"handled": False, "reason": "no plan mapping"}

    if etype == "customer.subscription.deleted":
        customer = obj.get("customer")
        email = accounts.email_for_customer(customer) if customer else None
        if not email:
            return {"handled": False, "reason": "unknown customer"}
        accounts.set_plan(email, "free")
        return {"handled": True, "action": "downgrade", "email": email, "plan": "free"}

    return {"handled": False, "reason": f"ignored event {etype}"}


def parse_event(payload: bytes) -> dict[str, Any]:
    try:
        event = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise StripeError(f"invalid webhook payload: {e}") from e
    if not isinstance(event, dict):
        raise StripeError("invalid webhook payload: not an object")
    return event


__all__ = [
    "StripeClient",
    "StripeError",
    "apply_webhook_event",
    "parse_event",
    "plan_for_price",
    "verify_webhook_signature",
]
