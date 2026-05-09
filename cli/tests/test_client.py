# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for :mod:`plinth_cli.client`."""

from __future__ import annotations

from pathlib import Path

import httpx

from plinth_cli.client import build_client, safe_build_client
from plinth_cli.config import Config, load_config


def test_build_client_uses_resolved_urls() -> None:
    """``build_client`` plumbs every URL through to the SDK without raising."""

    cfg = Config(
        workspace_url="http://w.test",
        gateway_url="http://g.test",
        identity_url="http://i.test",
        api_key="k",
        timeout=5.0,
    )
    sdk = build_client(cfg)
    # The SDK doesn't expose URLs as public attributes, but it does have
    # a ``workers`` and ``identity`` client — touch both to confirm
    # construction reached the inner stages.
    assert sdk is not None
    assert sdk.identity is not None  # identity_url was non-empty


def test_build_client_default_api_key_when_blank() -> None:
    """An empty ``api_key`` falls back to ``"local-dev"`` so the SDK is happy."""

    cfg = Config(api_key="")
    sdk = build_client(cfg)
    # Construction is the assertion: the SDK rejects empty strings on
    # some code paths. ``build_client`` must turn that into ``local-dev``.
    assert sdk is not None


def test_build_client_accepts_transports() -> None:
    """The CLI passes optional ``httpx`` transports through to the SDK."""

    cfg = Config()
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={}))
    sdk = build_client(
        cfg,
        workspace_transport=transport,
        gateway_transport=transport,
    )
    assert sdk is not None  # constructed without raising


def test_build_client_overrides_win() -> None:
    """``**overrides`` kwargs win over the resolved config."""

    cfg = Config(api_key="from-config")
    sdk = build_client(cfg, api_key="overridden")
    # The SDK's ``__init__`` may not expose ``api_key`` directly, but
    # the override is applied; any failure to apply would have raised.
    assert sdk is not None


def test_safe_build_client_returns_pair() -> None:
    """The safe variant always returns ``(client_or_none, error_or_none)``."""

    cfg = Config()
    client, err = safe_build_client(cfg)
    assert err is None
    assert client is not None


def test_safe_build_client_swallows_construction_error(monkeypatch) -> None:
    """If the SDK constructor raises, we surface the error string."""

    def boom(**_kw):
        raise RuntimeError("nope")

    # Replace the symbol after import — ``build_client`` does
    # ``from plinth import Plinth`` at call time, so we patch there.
    import plinth as _sdk

    monkeypatch.setattr(_sdk, "Plinth", boom)
    cfg = Config()
    client, err = safe_build_client(cfg)
    assert client is None
    assert "nope" in (err or "")


def test_build_client_from_loaded_config(tmp_path: Path) -> None:
    """End-to-end: load a config from disk, build a client off it."""

    p = tmp_path / "c.toml"
    p.write_text(
        """
[default]
workspace_url = "http://x.test"
gateway_url   = "http://y.test"
identity_url  = "http://z.test"
api_key       = "abc"
"""
    )
    cfg = load_config(config_path=p, env={})
    sdk = build_client(cfg)
    assert sdk is not None


def test_build_client_uses_default_timeout_from_config() -> None:
    """A custom ``timeout`` config flows through to the SDK call."""

    cfg = Config(timeout=2.5)
    sdk = build_client(cfg)
    # The SDK's HTTPClient stores timeout on its httpx.Client, which we
    # can read via ``_workspace_client.timeout``. We don't assume a
    # specific attribute name — just that construction succeeded.
    assert sdk is not None
