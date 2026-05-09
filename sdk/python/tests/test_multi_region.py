# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the v1.0 multi-region failover behaviour in the Python SDK."""

from __future__ import annotations

import httpx
import pytest

from plinth import Plinth
from plinth._http import HTTPClient
from plinth.exceptions import PlinthError, WorkspaceNotFound

WORKSPACE_PRIMARY = "http://workspace-eu.test"
WORKSPACE_FALLBACK = "http://workspace-us.test"
GATEWAY_PRIMARY = "http://gateway-eu.test"
GATEWAY_FALLBACK = "http://gateway-us.test"


def _make_dispatcher(
    handlers: dict[str, callable],
) -> httpx.MockTransport:
    """Build a MockTransport that routes by base host."""

    def handler(request: httpx.Request) -> httpx.Response:
        for prefix, fn in handlers.items():
            if str(request.url).startswith(prefix):
                return fn(request)
        return httpx.Response(404, text=f"no handler for {request.url}")

    return httpx.MockTransport(handler)


def test_plinth_accepts_region_and_fallback_args() -> None:
    client = Plinth(
        api_key="k",
        workspace_url=WORKSPACE_PRIMARY,
        gateway_url=GATEWAY_PRIMARY,
        region="eu-west-1",
        fallback_regions=["us-east-1"],
        fallback_workspace_urls={"us-east-1": WORKSPACE_FALLBACK},
        fallback_gateway_urls={"us-east-1": GATEWAY_FALLBACK},
    )
    candidates = client._workspace_http._candidates_in_order()
    assert candidates == [
        ("eu-west-1", WORKSPACE_PRIMARY),
        ("us-east-1", WORKSPACE_FALLBACK),
    ]
    client.close()


def test_plinth_fallback_without_region_works() -> None:
    """``fallback_regions`` without ``region`` still composes."""

    client = Plinth(
        api_key="k",
        workspace_url=WORKSPACE_PRIMARY,
        gateway_url=GATEWAY_PRIMARY,
        fallback_regions=["us-east-1"],
        fallback_workspace_urls={"us-east-1": WORKSPACE_FALLBACK},
        fallback_gateway_urls={"us-east-1": GATEWAY_FALLBACK},
    )
    candidates = client._workspace_http._candidates_in_order()
    assert candidates[0][0] == "<primary>"
    assert candidates[1] == ("us-east-1", WORKSPACE_FALLBACK)
    client.close()


def test_fallback_unknown_region_dropped() -> None:
    """Regions in ``fallback_regions`` without a URL entry are silently dropped."""

    client = Plinth(
        api_key="k",
        workspace_url=WORKSPACE_PRIMARY,
        gateway_url=GATEWAY_PRIMARY,
        region="eu",
        fallback_regions=["us", "ap"],
        # Only US has a workspace URL.
        fallback_workspace_urls={"us": WORKSPACE_FALLBACK},
        # Both have gateway URLs.
        fallback_gateway_urls={
            "us": GATEWAY_FALLBACK,
            "ap": "http://gateway-ap.test",
        },
    )
    ws_candidates = client._workspace_http._candidates_in_order()
    gw_candidates = client._gateway_http._candidates_in_order()
    assert [c[0] for c in ws_candidates] == ["eu", "us"]
    assert [c[0] for c in gw_candidates] == ["eu", "us", "ap"]
    client.close()


def test_no_fallback_raises_original_error() -> None:
    """Without fallbacks, a connection error surfaces immediately."""

    def primary(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    transport = _make_dispatcher({WORKSPACE_PRIMARY: primary})
    http = HTTPClient(WORKSPACE_PRIMARY, "k", transport=transport)
    try:
        with pytest.raises(PlinthError):
            http.get("/v1/workspaces")
    finally:
        http.close()


def test_failover_on_connection_error() -> None:
    """A primary connection error → SDK retries the fallback URL."""

    primary_calls = {"n": 0}
    fallback_calls = {"n": 0}

    def primary(request: httpx.Request) -> httpx.Response:
        primary_calls["n"] += 1
        raise httpx.ConnectError("primary down")

    def fallback(request: httpx.Request) -> httpx.Response:
        fallback_calls["n"] += 1
        return httpx.Response(200, json={"workspaces": []})

    transport = _make_dispatcher({
        WORKSPACE_PRIMARY: primary,
        WORKSPACE_FALLBACK: fallback,
    })
    http = HTTPClient(
        WORKSPACE_PRIMARY,
        "k",
        transport=transport,
        fallback_urls={"us": WORKSPACE_FALLBACK},
        primary_region="eu",
    )
    try:
        resp = http.get("/v1/workspaces")
        assert resp.status_code == 200
    finally:
        http.close()

    assert primary_calls["n"] == 1
    assert fallback_calls["n"] == 1


def test_failover_on_503() -> None:
    """A 503 from the primary triggers failover too."""

    def primary(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="overloaded")

    def fallback(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"workspaces": []})

    transport = _make_dispatcher({
        WORKSPACE_PRIMARY: primary,
        WORKSPACE_FALLBACK: fallback,
    })
    http = HTTPClient(
        WORKSPACE_PRIMARY,
        "k",
        transport=transport,
        fallback_urls={"us": WORKSPACE_FALLBACK},
        primary_region="eu",
    )
    try:
        resp = http.get("/v1/workspaces")
        assert resp.status_code == 200
    finally:
        http.close()


def test_409_redirects_to_named_primary() -> None:
    """A 409 with X-Plinth-Primary-Region routes to that region's URL."""

    primary_hits = {"n": 0}
    fallback_hits = {"n": 0}

    def primary(request: httpx.Request) -> httpx.Response:
        primary_hits["n"] += 1
        return httpx.Response(
            409,
            headers={"X-Plinth-Primary-Region": "us"},
            json={"error": {"code": "REPLICA_READ_ONLY", "message": "go elsewhere"}},
        )

    def fallback(request: httpx.Request) -> httpx.Response:
        fallback_hits["n"] += 1
        # Return success on the *retried* URL.
        return httpx.Response(201, json={"id": "ws_1", "name": "x"})

    transport = _make_dispatcher({
        WORKSPACE_PRIMARY: primary,
        WORKSPACE_FALLBACK: fallback,
    })
    http = HTTPClient(
        WORKSPACE_PRIMARY,
        "k",
        transport=transport,
        fallback_urls={"us": WORKSPACE_FALLBACK},
        primary_region="eu",
    )
    try:
        resp = http.post("/v1/workspaces", json={"name": "x"})
        assert resp.status_code == 201
    finally:
        http.close()

    assert primary_hits["n"] == 1
    assert fallback_hits["n"] == 1


def test_409_without_known_region_surfaces_error() -> None:
    """A 409 redirect to an unknown region surfaces the 409 to the caller."""

    def primary(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            headers={"X-Plinth-Primary-Region": "ap"},
            json={
                "error": {
                    "code": "REPLICA_READ_ONLY",
                    "message": "primary lives in ap",
                    "details": {},
                }
            },
        )

    transport = _make_dispatcher({WORKSPACE_PRIMARY: primary})
    http = HTTPClient(
        WORKSPACE_PRIMARY,
        "k",
        transport=transport,
        # Note: no ``ap`` in fallbacks.
        fallback_urls={"us": WORKSPACE_FALLBACK},
        primary_region="eu",
    )
    try:
        with pytest.raises(PlinthError):
            http.post("/v1/workspaces", json={"name": "x"})
    finally:
        http.close()


def test_get_works_through_facade_with_failover() -> None:
    """End-to-end: ``Plinth.list_workspaces`` exercises failover."""

    def primary(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("primary down")

    def fallback(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"workspaces": []})

    workspace_transport = _make_dispatcher({
        WORKSPACE_PRIMARY: primary,
        WORKSPACE_FALLBACK: fallback,
    })
    gateway_transport = _make_dispatcher({
        GATEWAY_PRIMARY: lambda r: httpx.Response(200, json={"tools": []})
    })
    client = Plinth(
        api_key="k",
        workspace_url=WORKSPACE_PRIMARY,
        gateway_url=GATEWAY_PRIMARY,
        region="eu",
        fallback_regions=["us"],
        fallback_workspace_urls={"us": WORKSPACE_FALLBACK},
        fallback_gateway_urls={"us": GATEWAY_FALLBACK},
        workspace_transport=workspace_transport,
        gateway_transport=gateway_transport,
    )
    try:
        result = client.list_workspaces()
        assert result == []
    finally:
        client.close()


def test_4xx_errors_not_retried() -> None:
    """A 404 from the primary is NOT retried against the fallback."""

    primary_hits = {"n": 0}
    fallback_hits = {"n": 0}

    def primary(request: httpx.Request) -> httpx.Response:
        primary_hits["n"] += 1
        return httpx.Response(
            404,
            json={"error": {"code": "WORKSPACE_NOT_FOUND", "message": "no", "details": {}}},
        )

    def fallback(request: httpx.Request) -> httpx.Response:
        fallback_hits["n"] += 1
        return httpx.Response(200, json={"id": "ws_1"})

    transport = _make_dispatcher({
        WORKSPACE_PRIMARY: primary,
        WORKSPACE_FALLBACK: fallback,
    })
    http = HTTPClient(
        WORKSPACE_PRIMARY,
        "k",
        transport=transport,
        fallback_urls={"us": WORKSPACE_FALLBACK},
        primary_region="eu",
    )
    try:
        with pytest.raises(WorkspaceNotFound):
            http.get(
                "/v1/workspaces/ws_x",
                not_found_class=WorkspaceNotFound,
            )
    finally:
        http.close()

    assert primary_hits["n"] == 1
    assert fallback_hits["n"] == 0


# ---------------------------------------------------------------------------
# 421 (Misdirected Request) + X-Plinth-Primary-URL behaviour


def test_421_redirects_to_named_primary() -> None:
    """A 421 with X-Plinth-Primary-Region routes to that region's URL."""

    primary_hits = {"n": 0}
    fallback_hits = {"n": 0}

    def primary(request: httpx.Request) -> httpx.Response:
        primary_hits["n"] += 1
        return httpx.Response(
            421,
            headers={
                "X-Plinth-Primary-Region": "us",
                "X-Plinth-Primary-URL": WORKSPACE_FALLBACK,
            },
            json={"error": {"code": "REPLICA_READ_ONLY", "message": "go elsewhere"}},
        )

    def fallback(request: httpx.Request) -> httpx.Response:
        fallback_hits["n"] += 1
        return httpx.Response(201, json={"id": "ws_1", "name": "x"})

    transport = _make_dispatcher({
        WORKSPACE_PRIMARY: primary,
        WORKSPACE_FALLBACK: fallback,
    })
    http = HTTPClient(
        WORKSPACE_PRIMARY,
        "k",
        transport=transport,
        fallback_urls={"us": WORKSPACE_FALLBACK},
        primary_region="eu",
    )
    try:
        resp = http.post("/v1/workspaces", json={"name": "x"})
        assert resp.status_code == 201
    finally:
        http.close()

    assert primary_hits["n"] == 1
    assert fallback_hits["n"] == 1


def test_421_url_hint_only_trusted_when_known() -> None:
    """A 421 with an unknown X-Plinth-Primary-URL is rejected (not followed)."""

    primary_hits = {"n": 0}

    def primary(request: httpx.Request) -> httpx.Response:
        primary_hits["n"] += 1
        return httpx.Response(
            421,
            headers={
                "X-Plinth-Primary-Region": "evil",
                # Hostile URL not in the SDK's candidate set.
                "X-Plinth-Primary-URL": "http://attacker.example",
            },
            json={
                "error": {
                    "code": "REPLICA_READ_ONLY",
                    "message": "go elsewhere",
                    "details": {},
                }
            },
        )

    transport = _make_dispatcher({WORKSPACE_PRIMARY: primary})
    http = HTTPClient(
        WORKSPACE_PRIMARY,
        "k",
        transport=transport,
        fallback_urls={"us": WORKSPACE_FALLBACK},
        primary_region="eu",
    )
    try:
        with pytest.raises(PlinthError):
            http.post("/v1/workspaces", json={"name": "x"})
    finally:
        http.close()

    # Only the primary was hit — the SDK refused to follow a hostile URL.
    assert primary_hits["n"] == 1


def test_421_redirect_loop_terminates() -> None:
    """Two replicas that bounce 421s at each other must NOT loop forever."""

    primary_hits = {"n": 0}
    fallback_hits = {"n": 0}

    def primary(request: httpx.Request) -> httpx.Response:
        primary_hits["n"] += 1
        return httpx.Response(
            421,
            headers={"X-Plinth-Primary-Region": "us"},
            json={"error": {"code": "REPLICA_READ_ONLY", "message": "go to us"}},
        )

    def fallback(request: httpx.Request) -> httpx.Response:
        fallback_hits["n"] += 1
        # Hostile bounce-back: the fallback also 421s pointing at "eu".
        return httpx.Response(
            421,
            headers={"X-Plinth-Primary-Region": "eu"},
            json={"error": {"code": "REPLICA_READ_ONLY", "message": "go to eu"}},
        )

    transport = _make_dispatcher({
        WORKSPACE_PRIMARY: primary,
        WORKSPACE_FALLBACK: fallback,
    })
    http = HTTPClient(
        WORKSPACE_PRIMARY,
        "k",
        transport=transport,
        fallback_urls={"us": WORKSPACE_FALLBACK},
        primary_region="eu",
    )
    try:
        with pytest.raises(PlinthError):
            http.post("/v1/workspaces", json={"name": "x"})
    finally:
        http.close()

    # Each URL is attempted exactly once, no matter how many bounces.
    assert primary_hits["n"] == 1
    assert fallback_hits["n"] == 1


def test_421_url_hint_to_known_fallback_is_trusted() -> None:
    """A 421 URL hint that matches a configured fallback is followed."""

    fallback_hits = {"n": 0}

    def primary(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            421,
            headers={
                "X-Plinth-Primary-Region": "us",
                "X-Plinth-Primary-URL": WORKSPACE_FALLBACK,
            },
            json={
                "error": {
                    "code": "REPLICA_READ_ONLY",
                    "message": "go",
                    "details": {},
                }
            },
        )

    def fallback(request: httpx.Request) -> httpx.Response:
        fallback_hits["n"] += 1
        return httpx.Response(201, json={"id": "ws_1", "name": "x"})

    transport = _make_dispatcher({
        WORKSPACE_PRIMARY: primary,
        WORKSPACE_FALLBACK: fallback,
    })
    http = HTTPClient(
        WORKSPACE_PRIMARY,
        "k",
        transport=transport,
        fallback_urls={"us": WORKSPACE_FALLBACK},
        primary_region="eu",
    )
    try:
        resp = http.post("/v1/workspaces", json={"name": "x"})
        assert resp.status_code == 201
    finally:
        http.close()

    assert fallback_hits["n"] == 1


def test_no_region_config_uses_workspace_url_directly() -> None:
    """Back-compat: without region/fallback args the SDK acts like v0.6."""

    hits = {"n": 0}

    def workspace_only(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        return httpx.Response(200, json={"workspaces": []})

    transport = _make_dispatcher({WORKSPACE_PRIMARY: workspace_only})
    # No fallback_urls, no primary_region.
    http = HTTPClient(WORKSPACE_PRIMARY, "k", transport=transport)
    try:
        resp = http.get("/v1/workspaces")
        assert resp.status_code == 200
    finally:
        http.close()

    candidates = http._candidates_in_order()
    # Just the primary, no fallback rows.
    assert candidates == [("<primary>", WORKSPACE_PRIMARY)]
    assert hits["n"] == 1
