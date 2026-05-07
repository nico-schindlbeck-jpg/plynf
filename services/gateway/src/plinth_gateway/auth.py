# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Mock OAuth pass-through for outbound tool calls.

For inbound auth we accept any non-empty ``Authorization: Bearer ...`` token.
For outbound auth we attach credentials based on the tool's ``auth_method``:

* ``none``   — no header
* ``bearer`` — ``Authorization: Bearer <auth_config["token"]>``
* ``oauth2`` — ``Authorization: Bearer <auth_config["mock_token"]>`` (v0.1 mock)
"""

from __future__ import annotations

from typing import Any

from .exceptions import Unauthorized


def check_inbound_auth(authorization: str | None) -> None:
    """Validate the inbound ``Authorization`` header.

    Args:
        authorization: raw header value (``None`` if absent).

    Raises:
        Unauthorized: if missing or not a non-empty bearer token.
    """
    if not authorization:
        raise Unauthorized("Missing Authorization header")
    parts = authorization.strip().split()
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        raise Unauthorized("Authorization must be 'Bearer <token>' with non-empty token")


def outbound_headers(
    auth_method: str, auth_config: dict[str, Any]
) -> dict[str, str]:
    """Return the headers to attach when calling a tool's backend.

    Args:
        auth_method: ``none`` | ``bearer`` | ``oauth2``.
        auth_config: tool's auth config dict.
    """
    if auth_method == "bearer":
        token = auth_config.get("token")
        if not token:
            return {}
        return {"Authorization": f"Bearer {token}"}
    if auth_method == "oauth2":
        token = auth_config.get("mock_token")
        if not token:
            return {}
        return {"Authorization": f"Bearer {token}"}
    return {}
