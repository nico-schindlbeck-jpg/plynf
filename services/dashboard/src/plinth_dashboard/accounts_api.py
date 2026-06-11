# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Self-serve API: signup, login, account info, checkout, webhook, key resolve.

The browser-facing routes live under ``/api/app/*`` (CORS'd to the site);
the proxy-facing key resolver lives under ``/internal/*`` behind a shared
secret. Together they implement the zero-terminal flow:

    POST /api/app/signup            create account (always starts on free)
    POST /api/app/login             email+password → session JWT
    GET  /api/app/me                account + API key for the onboarding wizard
    POST /api/app/keys/rotate       rotate the proxy API key
    POST /api/app/billing/checkout  Stripe Checkout URL (or simulated upgrade)
    POST /api/app/billing/portal    Stripe Customer-Portal URL
    POST /api/stripe/webhook        subscription lifecycle → plan changes
    GET  /internal/keys/{key}       api_key → tenant/tier (proxy auth source)
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .accounts import AccountError, AccountStore
from .billing_stripe import (
    StripeClient,
    StripeError,
    apply_webhook_event,
    parse_event,
    verify_webhook_signature,
)
from .ratelimit import SlidingWindowLimiter, client_key
from .settings import Settings


def _err(message: str, status: int = 400, code: str = "INVALID_ARGUMENTS") -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "details": {}}},
    )


async def _mint_session(
    request: Request, settings: Settings, *, subject: str, tenant: str
) -> dict[str, Any]:
    """Mint a dashboard session JWT via the identity service.

    Mirrors POST /api/app/session: real token when identity is reachable,
    demo token when it isn't and auth isn't enforced.
    """
    id_url = settings.identity_url.rstrip("/")
    overview = getattr(request.app.state, "overview", None)
    http = getattr(overview, "client", None) if overview else None
    if http is not None:
        try:
            resp = await http.post(
                f"{id_url}/v1/tokens",
                json={
                    "agent_id": subject,
                    "tenant_id": tenant,
                    "scopes": ["dashboard:read", "dashboard:write"],
                    "ttl_seconds": settings.app_session_ttl_seconds,
                },
            )
            if resp.status_code < 400:
                data = resp.json()
                return {"token": data.get("token"), "claims": data.get("claims", {})}
        except (httpx.HTTPError, ValueError):
            pass
    if settings.app_auth_required:
        raise StripeError("identity unavailable")  # mapped to 503 by callers
    return {"token": "demo", "claims": {"sub": subject, "tenant_id": tenant, "demo": True}}


def register_accounts_api(  # noqa: C901 - route table, intentionally flat
    app: FastAPI, settings: Settings, accounts: AccountStore
) -> None:
    stripe = StripeClient(settings.stripe_secret_key)
    site = settings.site_url.rstrip("/")
    price_for_plan = {
        "pro": settings.stripe_price_pro,
        "enterprise": settings.stripe_price_enterprise,
    }
    price_to_plan = {v: k for k, v in price_for_plan.items() if v}

    # -- signup / login ------------------------------------------------------

    # Per-IP sliding windows: generous enough for humans + the test suite,
    # hostile to credential stuffing / signup spam. Limits are settings so
    # ops can tune without a deploy.
    signup_limiter = SlidingWindowLimiter(
        limit=settings.auth_rate_limit_signup, window_s=settings.auth_rate_window_s
    )
    login_limiter = SlidingWindowLimiter(
        limit=settings.auth_rate_limit_login, window_s=settings.auth_rate_window_s
    )

    def _rate_limited(limiter: SlidingWindowLimiter, request: Request) -> JSONResponse | None:
        key = client_key(request)
        if limiter.allow(key):
            return None
        retry = limiter.retry_after_s(key)
        resp = _err(
            "Too many attempts — please wait a moment and try again.",
            429,
            "RATE_LIMITED",
        )
        resp.headers["Retry-After"] = str(retry)
        return resp

    @app.post("/api/app/signup", tags=["accounts"])
    async def signup(request: Request) -> JSONResponse:
        limited = _rate_limited(signup_limiter, request)
        if limited is not None:
            return limited
        body = await request.json()
        try:
            account = accounts.create(
                str(body.get("email") or ""),
                str(body.get("password") or ""),
                str(body.get("plan") or "free"),
            )
        except AccountError as e:
            return _err(str(e), 409 if "already exists" in str(e) else 400)
        try:
            session = await _mint_session(
                request, settings, subject=account["email"], tenant=account["tenant_id"]
            )
        except StripeError:
            return _err("Could not issue a session token.", 503, "IDENTITY_UNAVAILABLE")
        return JSONResponse(
            {"ok": True, "account": accounts.public_view(account), **session}
        )

    @app.post("/api/app/login", tags=["accounts"])
    async def login(request: Request) -> JSONResponse:
        limited = _rate_limited(login_limiter, request)
        if limited is not None:
            return limited
        body = await request.json()
        account = accounts.verify_login(
            str(body.get("email") or ""), str(body.get("password") or "")
        )
        if account is None:
            return _err("Invalid email or password.", 401, "UNAUTHENTICATED")
        try:
            session = await _mint_session(
                request, settings, subject=account["email"], tenant=account["tenant_id"]
            )
        except StripeError:
            return _err("Could not issue a session token.", 503, "IDENTITY_UNAVAILABLE")
        return JSONResponse(
            {"ok": True, "account": accounts.public_view(account), **session}
        )

    # -- account info / key rotation -----------------------------------------

    async def _authed_email(request: Request, client_email: str | None) -> str | None:
        """The email these sensitive routes operate on.

        SECURITY: when auth is enforced (production), the ONLY source of
        identity is the verified session token — never the client-supplied
        email. Otherwise any logged-in user could read or rotate another
        account's ``api_key`` simply by passing ``?email=victim``. In
        demo/test mode (auth not required, no identity service) we fall back
        to the supplied email so the open offline flow still works.
        """
        if not settings.app_auth_required:
            e = (client_email or "").strip().lower()
            return e or None
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        if not token:
            return None
        overview = getattr(request.app.state, "overview", None)
        http = getattr(overview, "client", None) if overview else None
        if http is None:
            return None
        id_url = settings.identity_url.rstrip("/")
        try:
            resp = await http.post(f"{id_url}/v1/tokens/verify", json={"token": token})
            if resp.status_code == 200:
                em = str(resp.json().get("agent_id") or "").strip().lower()
                return em or None
        except (httpx.HTTPError, ValueError):
            pass
        return None

    @app.get("/api/app/me", tags=["accounts"])
    async def me(request: Request) -> JSONResponse:
        email = await _authed_email(request, dict(request.query_params).get("email"))
        account = accounts.get(email) if email else None
        if account is None:
            return _err("Unknown account.", 404, "NOT_FOUND")
        return JSONResponse({"ok": True, "account": accounts.public_view(account)})

    @app.post("/api/app/keys/rotate", tags=["accounts"])
    async def rotate(request: Request) -> JSONResponse:
        body = await request.json()
        email = await _authed_email(request, body.get("email"))
        account = accounts.rotate_key(email) if email else None
        if account is None:
            return _err("Unknown account.", 404, "NOT_FOUND")
        return JSONResponse({"ok": True, "account": accounts.public_view(account)})

    # -- billing --------------------------------------------------------------

    @app.post("/api/app/billing/checkout", tags=["accounts"])
    async def checkout(request: Request) -> JSONResponse:
        body = await request.json()
        plan = str(body.get("plan") or "").strip().lower()
        email = await _authed_email(request, body.get("email"))
        account = accounts.get(email) if email else None
        if account is None:
            return _err("Unknown account — sign up first.", 404, "NOT_FOUND")
        if plan not in ("pro", "enterprise"):
            return _err("plan must be pro or enterprise")

        success_url = f"{site}/app/welcome?checkout=success&plan={plan}"
        cancel_url = f"{site}/app/welcome?checkout=cancelled"

        if not stripe.configured:
            # Simulated billing (no Stripe configured): flip the plan directly
            # so the demo/preview flow is complete end-to-end.
            accounts.set_plan(account["email"], plan)
            return JSONResponse({"ok": True, "url": success_url, "simulated": True})

        price_id = price_for_plan.get(plan) or ""
        if not price_id:
            return _err(f"No Stripe price configured for plan {plan!r}.", 503, "MISCONFIGURED")
        try:
            session = await stripe.create_checkout_session(
                price_id=price_id,
                customer_email=account["email"],
                success_url=success_url,
                cancel_url=cancel_url,
                plan=plan,
            )
        except StripeError as e:
            return _err(str(e), 502, "STRIPE_ERROR")
        return JSONResponse({"ok": True, "url": session.get("url"), "simulated": False})

    @app.post("/api/app/billing/portal", tags=["accounts"])
    async def portal(request: Request) -> JSONResponse:
        body = await request.json()
        email = await _authed_email(request, body.get("email"))
        account = accounts.get(email) if email else None
        if account is None:
            return _err("Unknown account.", 404, "NOT_FOUND")
        if not stripe.configured or not account.get("stripe_customer_id"):
            return _err("No billing profile yet.", 400, "NO_BILLING")
        try:
            session = await stripe.create_portal_session(
                customer_id=account["stripe_customer_id"],
                return_url=f"{site}/app/billing",
            )
        except StripeError as e:
            return _err(str(e), 502, "STRIPE_ERROR")
        return JSONResponse({"ok": True, "url": session.get("url")})

    # -- Stripe webhook (outside /api/app/* → not behind session auth) --------

    @app.post("/api/stripe/webhook", tags=["accounts"])
    async def stripe_webhook(request: Request) -> JSONResponse:
        payload = await request.body()
        if settings.stripe_webhook_secret:
            sig = request.headers.get("stripe-signature", "")
            if not verify_webhook_signature(payload, sig, settings.stripe_webhook_secret):
                return _err("Invalid webhook signature.", 400, "BAD_SIGNATURE")
        elif stripe.configured:
            # Stripe is live but no webhook secret set — refuse to apply
            # unauthenticated plan changes.
            return _err("Webhook secret not configured.", 503, "MISCONFIGURED")
        try:
            event = parse_event(payload)
        except StripeError as e:
            return _err(str(e))
        result = apply_webhook_event(event, accounts, price_to_plan)
        return JSONResponse({"ok": True, **result})

    # -- proxy key resolver (shared secret) -----------------------------------

    @app.get("/internal/keys/{api_key}", tags=["internal"])
    async def resolve_key(api_key: str, request: Request) -> JSONResponse:
        if not settings.internal_secret:
            return _err("Internal resolver not configured.", 503, "MISCONFIGURED")
        provided = request.headers.get("x-internal-secret", "")
        import hmac as _hmac

        if not _hmac.compare_digest(provided, settings.internal_secret):
            return _err("Forbidden.", 403, "FORBIDDEN")
        resolved = accounts.resolve_key(api_key)
        if resolved is None:
            return _err("Unknown key.", 404, "NOT_FOUND")
        return JSONResponse(resolved)


__all__ = ["register_accounts_api"]
