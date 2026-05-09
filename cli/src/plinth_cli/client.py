# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Helpers that build :class:`plinth.Plinth` SDK clients from CLI config.

The CLI delegates most of its real work to the Python SDK. This module
keeps the construction logic in one place so commands and tests can
build a client without duplicating the arg-mapping boilerplate, and so
``plinth_cli.main.CLIContext`` has a single, easily-mockable factory.

Resolution priority is *already* applied by :mod:`plinth_cli.config`;
:func:`build_client` just turns a resolved :class:`Config` into a live
SDK facade. If callers want to override one or two fields (e.g. swap in
an ``httpx`` transport for tests), pass them as kwargs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import-only typing
    import httpx

    from .config import Config


def build_client(
    config: "Config",
    *,
    workspace_transport: "httpx.BaseTransport | None" = None,
    gateway_transport: "httpx.BaseTransport | None" = None,
    identity_transport: "httpx.BaseTransport | None" = None,
    **overrides: Any,
) -> Any:
    """Construct a :class:`plinth.Plinth` SDK facade from ``config``.

    Imports are deferred so ``plinth --help`` keeps working even if the
    SDK isn't installed (rare, but handy when shipping the CLI alone).

    Args:
        config: Resolved CLI config (see :mod:`plinth_cli.config`).
        workspace_transport: Optional ``httpx`` transport for the workspace
            HTTP client. Used by tests to plug in ``respx``.
        gateway_transport: Optional ``httpx`` transport for the gateway.
        identity_transport: Optional ``httpx`` transport for identity.
        **overrides: Forwarded to the :class:`Plinth` constructor verbatim
            (last-wins). Keeps the helper future-proof against new SDK
            kwargs without forcing a CLI release.

    Returns:
        A live SDK client. The CLI never closes it explicitly — Click's
        process exits and the ``httpx`` clients clean up their sockets.
    """

    # Defer the SDK import; the CLI ships it but ``--help`` should not.
    from plinth import Plinth

    kwargs: dict[str, Any] = {
        "workspace_url": config.workspace_url,
        "gateway_url": config.gateway_url,
        "identity_url": config.identity_url,
        "api_key": config.api_key or "local-dev",
        "timeout": config.timeout,
    }
    if workspace_transport is not None:
        kwargs["workspace_transport"] = workspace_transport
    if gateway_transport is not None:
        kwargs["gateway_transport"] = gateway_transport
    if identity_transport is not None:
        kwargs["identity_transport"] = identity_transport
    kwargs.update(overrides)
    return Plinth(**kwargs)


def safe_build_client(config: "Config", **kwargs: Any) -> tuple[Any | None, str | None]:
    """Same as :func:`build_client` but returns ``(client, error)`` instead.

    Useful for diagnostic commands (``plinth health``, ``plinth config
    show``) that should report a "couldn't reach SDK" failure without
    crashing the entire process.
    """

    try:
        return build_client(config, **kwargs), None
    except Exception as exc:  # broad: surfacing the message is the point
        return None, str(exc)


__all__ = ["build_client", "safe_build_client"]
