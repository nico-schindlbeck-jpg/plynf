# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""End-to-end FastAPI tests for the workspace service.

Every test goes through ``httpx.AsyncClient`` against the ASGI app, so we
exercise routing, middleware, exception handlers, and serialisation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from plinth_workspace.api import create_app
from plinth_workspace.db import init_db
from plinth_workspace.settings import Settings

# asyncio_mode=auto in pyproject.toml means async tests don't need a mark.


# ---------------------------------------------------------------------------
# Health


async def test_healthz(client: httpx.AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "workspace"
    assert body["version"]


# ---------------------------------------------------------------------------
# Workspaces


async def test_create_workspace(client: httpx.AsyncClient) -> None:
    resp = await client.post("/v1/workspaces", json={"name": "demo"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"].startswith("ws_")
    assert body["name"] == "demo"
    assert body["metadata"] == {}


async def test_create_workspace_validation(client: httpx.AsyncClient) -> None:
    resp = await client.post("/v1/workspaces", json={})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_ARGUMENTS"


async def test_list_workspaces(client: httpx.AsyncClient) -> None:
    await client.post("/v1/workspaces", json={"name": "a"})
    await client.post("/v1/workspaces", json={"name": "b"})
    resp = await client.get("/v1/workspaces")
    assert resp.status_code == 200
    assert len(resp.json()["workspaces"]) == 2


async def test_get_workspace(client: httpx.AsyncClient, workspace_id: str) -> None:
    resp = await client.get(f"/v1/workspaces/{workspace_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == workspace_id


async def test_get_workspace_404(client: httpx.AsyncClient) -> None:
    resp = await client.get("/v1/workspaces/ws_nope")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "WORKSPACE_NOT_FOUND"


async def test_delete_workspace(client: httpx.AsyncClient, workspace_id: str) -> None:
    resp = await client.delete(f"/v1/workspaces/{workspace_id}")
    assert resp.status_code == 204
    resp2 = await client.get(f"/v1/workspaces/{workspace_id}")
    assert resp2.status_code == 404


# ---------------------------------------------------------------------------
# KV


async def test_kv_put_and_get(client: httpx.AsyncClient, workspace_id: str) -> None:
    resp = await client.put(
        f"/v1/workspaces/{workspace_id}/kv/topic",
        json={"value": "wind"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == 1
    assert body["value"] == "wind"

    resp2 = await client.put(
        f"/v1/workspaces/{workspace_id}/kv/topic",
        json={"value": "solar"},
    )
    assert resp2.json()["version"] == 2

    resp3 = await client.get(f"/v1/workspaces/{workspace_id}/kv/topic")
    assert resp3.json()["value"] == "solar"

    resp4 = await client.get(f"/v1/workspaces/{workspace_id}/kv/topic?version=1")
    assert resp4.json()["value"] == "wind"


async def test_kv_get_missing(client: httpx.AsyncClient, workspace_id: str) -> None:
    resp = await client.get(f"/v1/workspaces/{workspace_id}/kv/missing")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "KEY_NOT_FOUND"


async def test_kv_history(client: httpx.AsyncClient, workspace_id: str) -> None:
    for i in range(3):
        await client.put(
            f"/v1/workspaces/{workspace_id}/kv/k",
            json={"value": i},
        )
    resp = await client.get(f"/v1/workspaces/{workspace_id}/kv/k/history")
    assert resp.status_code == 200
    versions = resp.json()["versions"]
    assert len(versions) == 3
    assert [v["version"] for v in versions] == [1, 2, 3]


async def test_kv_history_missing(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    resp = await client.get(f"/v1/workspaces/{workspace_id}/kv/ghost/history")
    assert resp.status_code == 404


async def test_kv_list(client: httpx.AsyncClient, workspace_id: str) -> None:
    await client.put(
        f"/v1/workspaces/{workspace_id}/kv/a", json={"value": 1}
    )
    await client.put(
        f"/v1/workspaces/{workspace_id}/kv/b", json={"value": 2}
    )
    resp = await client.get(f"/v1/workspaces/{workspace_id}/kv")
    assert resp.status_code == 200
    keys = sorted(e["key"] for e in resp.json()["entries"])
    assert keys == ["a", "b"]


async def test_kv_delete(client: httpx.AsyncClient, workspace_id: str) -> None:
    await client.put(
        f"/v1/workspaces/{workspace_id}/kv/k", json={"value": "v"}
    )
    resp = await client.delete(f"/v1/workspaces/{workspace_id}/kv/k")
    assert resp.status_code == 204

    resp2 = await client.get(f"/v1/workspaces/{workspace_id}/kv/k")
    assert resp2.status_code == 404
    history = await client.get(f"/v1/workspaces/{workspace_id}/kv/k/history")
    assert history.status_code == 200
    versions = history.json()["versions"]
    assert versions[-1]["deleted"] is True


async def test_kv_get_specific_missing_version(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    await client.put(f"/v1/workspaces/{workspace_id}/kv/k", json={"value": "v"})
    resp = await client.get(f"/v1/workspaces/{workspace_id}/kv/k?version=99")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Files


async def test_file_put_and_get(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    resp = await client.put(
        f"/v1/workspaces/{workspace_id}/files/report.md",
        content=b"# Hi",
        headers={"Content-Type": "text/markdown"},
    )
    assert resp.status_code == 200
    meta = resp.json()
    assert meta["size"] == 4
    assert meta["content_type"] == "text/markdown"
    assert meta["version"] == 1
    assert meta["sha256"]

    resp2 = await client.get(f"/v1/workspaces/{workspace_id}/files/report.md")
    assert resp2.status_code == 200
    assert resp2.content == b"# Hi"
    assert resp2.headers["x-plinth-version"] == "1"
    assert resp2.headers["x-plinth-sha256"] == meta["sha256"]


async def test_file_meta(client: httpx.AsyncClient, workspace_id: str) -> None:
    await client.put(
        f"/v1/workspaces/{workspace_id}/files/data.bin",
        content=b"\x00\x01\x02",
    )
    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/files/data.bin/meta"
    )
    assert resp.status_code == 200
    assert resp.json()["size"] == 3


async def test_file_versioning(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    await client.put(
        f"/v1/workspaces/{workspace_id}/files/v.txt", content=b"v1"
    )
    await client.put(
        f"/v1/workspaces/{workspace_id}/files/v.txt", content=b"v2"
    )
    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/files/v.txt?version=1"
    )
    assert resp.status_code == 200
    assert resp.content == b"v1"


async def test_file_delete(client: httpx.AsyncClient, workspace_id: str) -> None:
    await client.put(
        f"/v1/workspaces/{workspace_id}/files/x.txt", content=b"y"
    )
    resp = await client.delete(f"/v1/workspaces/{workspace_id}/files/x.txt")
    assert resp.status_code == 204
    miss = await client.get(f"/v1/workspaces/{workspace_id}/files/x.txt")
    assert miss.status_code == 404


async def test_file_list(client: httpx.AsyncClient, workspace_id: str) -> None:
    await client.put(
        f"/v1/workspaces/{workspace_id}/files/a.txt", content=b"1"
    )
    await client.put(
        f"/v1/workspaces/{workspace_id}/files/b.txt", content=b"2"
    )
    resp = await client.get(f"/v1/workspaces/{workspace_id}/files")
    assert resp.status_code == 200
    paths = sorted(f["path"] for f in resp.json()["files"])
    assert paths == ["a.txt", "b.txt"]


async def test_file_missing(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    resp = await client.get(f"/v1/workspaces/{workspace_id}/files/ghost.txt")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "FILE_NOT_FOUND"


async def test_file_meta_missing(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/files/ghost.txt/meta"
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Snapshots


async def test_create_and_list_snapshots(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    await client.put(
        f"/v1/workspaces/{workspace_id}/kv/topic", json={"value": "wind"}
    )
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/snapshots",
        json={"name": "first", "message": "baseline"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"].startswith("snap_")
    assert body["kv_versions"] == {"topic": 1}

    listed = await client.get(f"/v1/workspaces/{workspace_id}/snapshots")
    assert listed.status_code == 200
    assert len(listed.json()["snapshots"]) == 1


async def test_get_snapshot(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    create = await client.post(
        f"/v1/workspaces/{workspace_id}/snapshots",
        json={"name": "x"},
    )
    snap_id = create.json()["id"]
    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/snapshots/{snap_id}"
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == snap_id


async def test_diff_snapshots(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    await client.put(
        f"/v1/workspaces/{workspace_id}/kv/k", json={"value": "v1"}
    )
    s1 = (
        await client.post(
            f"/v1/workspaces/{workspace_id}/snapshots", json={"name": "a"}
        )
    ).json()
    await client.put(
        f"/v1/workspaces/{workspace_id}/kv/k", json={"value": "v2"}
    )
    await client.put(
        f"/v1/workspaces/{workspace_id}/kv/new", json={"value": 1}
    )
    s2 = (
        await client.post(
            f"/v1/workspaces/{workspace_id}/snapshots", json={"name": "b"}
        )
    ).json()

    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/snapshots/{s1['id']}/diff",
        params={"against": s2["id"]},
    )
    assert resp.status_code == 200
    diff = resp.json()
    assert "new" in diff["kv_added"]
    assert "k" in diff["kv_modified"]


async def test_diff_missing_against_param(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    s = (
        await client.post(
            f"/v1/workspaces/{workspace_id}/snapshots", json={"name": "a"}
        )
    ).json()
    resp = await client.get(
        f"/v1/workspaces/{workspace_id}/snapshots/{s['id']}/diff"
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Branches


async def test_branch_lifecycle(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    # Set up baseline + snapshot.
    await client.put(
        f"/v1/workspaces/{workspace_id}/kv/topic",
        json={"value": "main-v1"},
    )
    snap = (
        await client.post(
            f"/v1/workspaces/{workspace_id}/snapshots",
            json={"name": "base"},
        )
    ).json()

    create = await client.post(
        f"/v1/workspaces/{workspace_id}/branches",
        json={"name": "exp", "from_snapshot": snap["id"]},
    )
    assert create.status_code == 201
    br = create.json()
    assert br["id"].startswith("br_")
    assert br["merged"] is False

    listed = await client.get(f"/v1/workspaces/{workspace_id}/branches")
    assert any(b["id"] == br["id"] for b in listed.json()["branches"])

    # Write on the branch — main is unchanged.
    await client.put(
        f"/v1/workspaces/{workspace_id}/kv/topic",
        params={"branch": br["id"]},
        json={"value": "branch-v1"},
    )
    main_get = await client.get(f"/v1/workspaces/{workspace_id}/kv/topic")
    assert main_get.json()["value"] == "main-v1"
    branch_get = await client.get(
        f"/v1/workspaces/{workspace_id}/kv/topic",
        params={"branch": br["id"]},
    )
    assert branch_get.json()["value"] == "branch-v1"

    merge = await client.post(
        f"/v1/workspaces/{workspace_id}/branches/{br['id']}/merge"
    )
    assert merge.status_code == 200
    body = merge.json()
    assert "topic" in body["kv_merged"]

    after = await client.get(f"/v1/workspaces/{workspace_id}/kv/topic")
    assert after.json()["value"] == "branch-v1"

    # Re-merge is rejected.
    again = await client.post(
        f"/v1/workspaces/{workspace_id}/branches/{br['id']}/merge"
    )
    assert again.status_code == 400


async def test_branch_create_from_missing_snapshot(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    resp = await client.post(
        f"/v1/workspaces/{workspace_id}/branches",
        json={"name": "x", "from_snapshot": "snap_nope"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "SNAPSHOT_NOT_FOUND"


async def test_branch_delete(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    snap = (
        await client.post(
            f"/v1/workspaces/{workspace_id}/snapshots", json={"name": "s"}
        )
    ).json()
    br = (
        await client.post(
            f"/v1/workspaces/{workspace_id}/branches",
            json={"name": "b", "from_snapshot": snap["id"]},
        )
    ).json()
    resp = await client.delete(
        f"/v1/workspaces/{workspace_id}/branches/{br['id']}"
    )
    assert resp.status_code == 204
    miss = await client.delete(
        f"/v1/workspaces/{workspace_id}/branches/{br['id']}"
    )
    assert miss.status_code == 404


# ---------------------------------------------------------------------------
# Auth


@pytest_asyncio.fixture()
async def auth_required_client(
    tmp_path: Path,
) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(
        data_dir=tmp_path / "auth-data",
        log_level="WARNING",
        auth_required=True,
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.blobs_dir.mkdir(parents=True, exist_ok=True)
    await init_db(settings.db_path)
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def test_auth_required_blocks_missing_token(
    auth_required_client: httpx.AsyncClient,
) -> None:
    resp = await auth_required_client.get("/v1/workspaces")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


async def test_auth_required_allows_with_token(
    auth_required_client: httpx.AsyncClient,
) -> None:
    resp = await auth_required_client.get(
        "/v1/workspaces",
        headers={"Authorization": "Bearer some-token"},
    )
    assert resp.status_code == 200


async def test_healthz_skips_auth(
    auth_required_client: httpx.AsyncClient,
) -> None:
    resp = await auth_required_client.get("/healthz")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Path / edge-case behaviour


async def test_kv_put_branch_404(
    client: httpx.AsyncClient, workspace_id: str
) -> None:
    resp = await client.put(
        f"/v1/workspaces/{workspace_id}/kv/k",
        params={"branch": "br_nope"},
        json={"value": "v"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "BRANCH_NOT_FOUND"


async def test_kv_put_workspace_404(client: httpx.AsyncClient) -> None:
    resp = await client.put(
        "/v1/workspaces/ws_nope/kv/k", json={"value": "v"}
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "WORKSPACE_NOT_FOUND"


async def test_request_id_echo(client: httpx.AsyncClient) -> None:
    resp = await client.get("/healthz", headers={"X-Request-Id": "abc"})
    assert resp.headers["x-request-id"] == "abc"


async def test_unknown_route_404(client: httpx.AsyncClient) -> None:
    resp = await client.get("/v1/does-not-exist")
    assert resp.status_code == 404
    body = resp.json()
    assert "error" in body


async def test_request_without_token_logs_but_succeeds(
    client: httpx.AsyncClient,
) -> None:
    """When auth_required=False, a missing token logs a warning but allows."""

    # Drop the default Bearer auth header by overriding for this request.
    resp = await client.get("/v1/workspaces", headers={"Authorization": ""})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Lazy app attribute on the api module


def test_api_app_attribute_lazy(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PLINTH_DATA_DIR", str(tmp_path / "lazy"))
    monkeypatch.setenv("PLINTH_LOG_LEVEL", "WARNING")
    # Importing the attribute triggers __getattr__ in api module.
    import plinth_workspace.api as api_module

    # Reset cached app so we exercise the lazy path.
    api_module._app = None  # type: ignore[attr-defined]
    app = api_module.app  # type: ignore[attr-defined]
    assert app is not None
    with pytest.raises(AttributeError):
        getattr(api_module, "does_not_exist")  # noqa: B009 — exercising __getattr__
