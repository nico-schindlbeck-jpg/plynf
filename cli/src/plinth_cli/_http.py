# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tiny HTTP helpers for direct calls (when the SDK doesn't cover something).

The SDK is the primary backend, but a few CLI-only endpoints — admin
migrations, /healthz, /metrics — aren't on the SDK surface. Going through
``httpx`` directly keeps the CLI self-contained without bloating the SDK.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx


def authed_client(
    base_url: str,
    api_key: str,
    *,
    timeout: float = 10.0,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Client:
    """Return an :class:`httpx.Client` with ``Authorization: Bearer`` set."""

    headers: dict[str, str] = {"User-Agent": "plinth-cli"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return httpx.Client(
        base_url=base_url,
        headers=headers,
        timeout=timeout,
        transport=transport,
    )


def get_json(
    client: httpx.Client,
    path: str,
    *,
    params: Mapping[str, Any] | None = None,
) -> tuple[int, Any]:
    """GET ``path`` and return ``(status_code, parsed_json_or_text)``.

    Errors don't raise — callers decide how to surface them so we don't
    have to special-case "service down" everywhere.
    """

    try:
        resp = client.get(path, params=_clean_params(params))
    except httpx.HTTPError as exc:
        return -1, {"error": str(exc)}
    return resp.status_code, _safe_json(resp)


def post_json(
    client: httpx.Client,
    path: str,
    *,
    json: Mapping[str, Any] | None = None,
    params: Mapping[str, Any] | None = None,
) -> tuple[int, Any]:
    """POST and return ``(status_code, payload)`` (``-1`` for transport errors)."""

    try:
        resp = client.post(path, json=dict(json or {}), params=_clean_params(params))
    except httpx.HTTPError as exc:
        return -1, {"error": str(exc)}
    return resp.status_code, _safe_json(resp)


def _safe_json(resp: httpx.Response) -> Any:
    """Return ``resp.json()`` if possible, else ``{"text": ...}``."""

    try:
        return resp.json()
    except ValueError:
        return {"text": resp.text}


def _clean_params(params: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Drop ``None`` values from ``params`` so they don't ship as ``"None"``."""

    if not params:
        return None
    return {k: v for k, v in params.items() if v is not None}


__all__ = ["authed_client", "get_json", "post_json"]
