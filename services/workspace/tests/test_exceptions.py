# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the exception handlers and PlinthError envelope."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, status
from httpx import ASGITransport
from starlette.exceptions import HTTPException

from plinth_workspace.api import create_app
from plinth_workspace.db import init_db
from plinth_workspace.exceptions import (
    BranchAlreadyMerged,
    BranchNotFound,
    FileNotFound,
    InvalidArguments,
    KeyNotFound,
    PlinthError,
    SnapshotNotFound,
    Unauthorized,
    WorkspaceNotFound,
    install_exception_handlers,
)
from plinth_workspace.settings import Settings

# ---------------------------------------------------------------------------
# PlinthError envelope (sync — no asyncio mark)


def test_plinth_error_defaults() -> None:
    e = PlinthError()
    assert e.code == "INTERNAL_ERROR"
    assert e.status_code == 500
    assert e.message == "internal error"
    assert e.details == {}


def test_plinth_error_overrides() -> None:
    e = PlinthError(
        "boom",
        details={"a": 1},
        code="MY_CODE",
        status_code=418,
    )
    assert e.message == "boom"
    assert e.code == "MY_CODE"
    assert e.status_code == 418
    assert e.details == {"a": 1}


def test_typed_404_classes() -> None:
    for exc_cls, kwargs, expected_code in [
        (WorkspaceNotFound, {"workspace_id": "ws_x"}, "WORKSPACE_NOT_FOUND"),
        (KeyNotFound, {"workspace_id": "ws_x", "key": "k"}, "KEY_NOT_FOUND"),
        (KeyNotFound, {"workspace_id": "ws_x", "key": "k", "version": 2}, "KEY_NOT_FOUND"),
        (FileNotFound, {"workspace_id": "ws_x", "path": "a"}, "FILE_NOT_FOUND"),
        (FileNotFound, {"workspace_id": "ws_x", "path": "a", "version": 2}, "FILE_NOT_FOUND"),
        (SnapshotNotFound, {"snapshot_id": "snap_x"}, "SNAPSHOT_NOT_FOUND"),
        (BranchNotFound, {"branch_id": "br_x"}, "BRANCH_NOT_FOUND"),
        (BranchAlreadyMerged, {"branch_id": "br_x"}, "BRANCH_ALREADY_MERGED"),
    ]:
        e = exc_cls(**kwargs)
        assert e.code == expected_code
        assert e.status_code in {400, 404}


def test_invalid_arguments_and_unauthorized() -> None:
    assert InvalidArguments().status_code == 400
    assert Unauthorized().status_code == 401


# ---------------------------------------------------------------------------
# Handler-level integration: spin up a tiny app with raising endpoints.


def _make_test_app() -> FastAPI:
    app = FastAPI()
    install_exception_handlers(app)

    @app.get("/raise/plinth")
    async def _raise_plinth() -> None:
        raise WorkspaceNotFound("ws_x")

    @app.get("/raise/http/{code}")
    async def _raise_http(code: int) -> None:
        raise HTTPException(status_code=code, detail=f"failure-{code}")

    @app.get("/raise/runtime")
    async def _raise_runtime() -> None:
        raise RuntimeError("kaboom")

    @app.get("/raise/http-no-detail/{code}")
    async def _raise_http_no_detail(code: int) -> None:
        raise HTTPException(status_code=code)

    return app


@pytest_asyncio.fixture()
async def handler_client() -> AsyncIterator[httpx.AsyncClient]:
    app = _make_test_app()
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c


async def test_handler_plinth_error(handler_client: httpx.AsyncClient) -> None:
    resp = await handler_client.get("/raise/plinth")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "WORKSPACE_NOT_FOUND"
    assert body["error"]["details"] == {"workspace_id": "ws_x"}


@pytest.mark.parametrize(
    "code,expected",
    [
        (404, "NOT_FOUND"),
        (401, "UNAUTHORIZED"),
        (400, "INVALID_ARGUMENTS"),
        (405, "METHOD_NOT_ALLOWED"),
        (429, "RATE_LIMITED"),
        (502, "INTERNAL_ERROR"),
        (418, "HTTP_ERROR"),
    ],
)
async def test_handler_http_codes(
    handler_client: httpx.AsyncClient, code: int, expected: str
) -> None:
    resp = await handler_client.get(f"/raise/http/{code}")
    assert resp.status_code == code
    assert resp.json()["error"]["code"] == expected


async def test_unhandled_handler_directly() -> None:
    """Exercise the catch-all handler without going through the transport."""

    from plinth_workspace.exceptions import unhandled_exception_handler

    fake_request = object()  # handler ignores it
    resp = await unhandled_exception_handler(fake_request, RuntimeError("oops"))  # type: ignore[arg-type]
    assert resp.status_code == 500
    assert b"INTERNAL_ERROR" in resp.body


# ---------------------------------------------------------------------------
# Auth-required happy + sad paths through the real app, using a clean tmpdir.


@pytest_asyncio.fixture()
async def real_app_client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(data_dir=tmp_path / "real", auth_required=True)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.blobs_dir.mkdir(parents=True, exist_ok=True)
    await init_db(settings.db_path)
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c


async def test_auth_with_empty_bearer(
    real_app_client: httpx.AsyncClient,
) -> None:
    resp = await real_app_client.get(
        "/v1/workspaces",
        headers={"Authorization": "Bearer   "},
    )
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED


async def test_auth_with_non_bearer(
    real_app_client: httpx.AsyncClient,
) -> None:
    resp = await real_app_client.get(
        "/v1/workspaces",
        headers={"Authorization": "Basic abc"},
    )
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED
