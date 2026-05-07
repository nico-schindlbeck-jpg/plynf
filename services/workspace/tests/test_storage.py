# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Direct unit tests for the storage layer (no FastAPI in the loop)."""

from __future__ import annotations

import pytest

from plinth_workspace.exceptions import (
    FileNotFound,
    KeyNotFound,
    WorkspaceNotFound,
)
from plinth_workspace.storage import WorkspaceStore

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Workspaces


async def test_create_and_get_workspace(store: WorkspaceStore) -> None:
    ws = await store.create_workspace("alpha", metadata={"owner": "nico"})
    assert ws.id.startswith("ws_")
    assert ws.name == "alpha"
    assert ws.metadata == {"owner": "nico"}

    fetched = await store.get_workspace(ws.id)
    assert fetched.id == ws.id
    assert fetched.metadata == {"owner": "nico"}


async def test_list_workspaces(store: WorkspaceStore) -> None:
    a = await store.create_workspace("a")
    b = await store.create_workspace("b")
    listed = await store.list_workspaces()
    assert {w.id for w in listed} == {a.id, b.id}


async def test_get_missing_workspace(store: WorkspaceStore) -> None:
    with pytest.raises(WorkspaceNotFound):
        await store.get_workspace("ws_nope")


async def test_delete_workspace_missing(store: WorkspaceStore) -> None:
    with pytest.raises(WorkspaceNotFound):
        await store.delete_workspace("ws_nope")


async def test_delete_workspace_cascades(store: WorkspaceStore) -> None:
    ws = await store.create_workspace("cascade")
    await store.kv_put(ws.id, "k", {"v": 1})
    await store.file_put(ws.id, "f.txt", b"hi", content_type="text/plain")
    await store.delete_workspace(ws.id)
    with pytest.raises(WorkspaceNotFound):
        await store.get_workspace(ws.id)
    # KV gets are also 404 because the workspace is gone.
    with pytest.raises(WorkspaceNotFound):
        await store.kv_get(ws.id, "k")


# ---------------------------------------------------------------------------
# KV


async def test_kv_put_increments_version(store: WorkspaceStore) -> None:
    ws = await store.create_workspace("kv-ws")
    e1 = await store.kv_put(ws.id, "topic", "wind")
    e2 = await store.kv_put(ws.id, "topic", "solar")
    assert e1.version == 1
    assert e2.version == 2
    latest = await store.kv_get(ws.id, "topic")
    assert latest.value == "solar"
    assert latest.version == 2


async def test_kv_get_specific_version(store: WorkspaceStore) -> None:
    ws = await store.create_workspace("kv-ws")
    await store.kv_put(ws.id, "k", "v1")
    await store.kv_put(ws.id, "k", "v2")
    e = await store.kv_get(ws.id, "k", version=1)
    assert e.value == "v1"


async def test_kv_get_missing_version(store: WorkspaceStore) -> None:
    ws = await store.create_workspace("kv-ws")
    await store.kv_put(ws.id, "k", "v")
    with pytest.raises(KeyNotFound):
        await store.kv_get(ws.id, "k", version=99)


async def test_kv_get_missing_key(store: WorkspaceStore) -> None:
    ws = await store.create_workspace("kv-ws")
    with pytest.raises(KeyNotFound):
        await store.kv_get(ws.id, "nope")


async def test_kv_delete_creates_tombstone(store: WorkspaceStore) -> None:
    ws = await store.create_workspace("kv-ws")
    await store.kv_put(ws.id, "k", "v")
    tomb = await store.kv_delete(ws.id, "k")
    assert tomb.deleted is True
    assert tomb.version == 2
    with pytest.raises(KeyNotFound):
        await store.kv_get(ws.id, "k")
    history = await store.kv_history(ws.id, "k")
    assert len(history) == 2
    assert history[-1].deleted is True


async def test_kv_delete_missing_is_404(store: WorkspaceStore) -> None:
    ws = await store.create_workspace("kv-ws")
    with pytest.raises(KeyNotFound):
        await store.kv_delete(ws.id, "ghost")


async def test_kv_delete_already_deleted(store: WorkspaceStore) -> None:
    ws = await store.create_workspace("kv-ws")
    await store.kv_put(ws.id, "k", "v")
    await store.kv_delete(ws.id, "k")
    with pytest.raises(KeyNotFound):
        await store.kv_delete(ws.id, "k")


async def test_kv_history_missing_key(store: WorkspaceStore) -> None:
    ws = await store.create_workspace("kv-ws")
    with pytest.raises(KeyNotFound):
        await store.kv_history(ws.id, "ghost")


async def test_kv_list_excludes_tombstones(store: WorkspaceStore) -> None:
    ws = await store.create_workspace("kv-ws")
    await store.kv_put(ws.id, "alive", 1)
    await store.kv_put(ws.id, "doomed", 2)
    await store.kv_delete(ws.id, "doomed")
    listed = await store.kv_list(ws.id)
    assert [e.key for e in listed] == ["alive"]


async def test_kv_complex_values(store: WorkspaceStore) -> None:
    ws = await store.create_workspace("complex")
    payload = {"a": [1, 2, 3], "b": {"nested": True}}
    await store.kv_put(ws.id, "doc", payload)
    fetched = await store.kv_get(ws.id, "doc")
    assert fetched.value == payload


# ---------------------------------------------------------------------------
# Files


async def test_file_put_and_get(store: WorkspaceStore) -> None:
    ws = await store.create_workspace("files-ws")
    e = await store.file_put(ws.id, "a/b/c.txt", b"hello", content_type="text/plain")
    assert e.version == 1
    assert e.size == 5
    assert e.content_type == "text/plain"
    assert e.sha256

    meta, data = await store.file_read(ws.id, "a/b/c.txt")
    assert data == b"hello"
    assert meta.sha256 == e.sha256


async def test_file_versioning(store: WorkspaceStore) -> None:
    ws = await store.create_workspace("files-ws")
    await store.file_put(ws.id, "f.txt", b"v1")
    await store.file_put(ws.id, "f.txt", b"v2")
    meta, data = await store.file_read(ws.id, "f.txt")
    assert data == b"v2"
    assert meta.version == 2

    _, data1 = await store.file_read(ws.id, "f.txt", version=1)
    assert data1 == b"v1"


async def test_file_content_type_detection(store: WorkspaceStore) -> None:
    ws = await store.create_workspace("files-ws")
    md = await store.file_put(ws.id, "report.md", b"# hi")
    assert md.content_type in {"text/markdown", "text/x-markdown"}
    img = await store.file_put(ws.id, "p.png", b"\x89PNG\r\n")
    assert img.content_type == "image/png"
    bin_ = await store.file_put(ws.id, "blob", b"\x00")
    assert bin_.content_type == "application/octet-stream"


async def test_file_delete(store: WorkspaceStore) -> None:
    ws = await store.create_workspace("files-ws")
    await store.file_put(ws.id, "x.txt", b"y")
    tomb = await store.file_delete(ws.id, "x.txt")
    assert tomb.deleted is True
    with pytest.raises(FileNotFound):
        await store.file_read(ws.id, "x.txt")


async def test_file_delete_missing(store: WorkspaceStore) -> None:
    ws = await store.create_workspace("files-ws")
    with pytest.raises(FileNotFound):
        await store.file_delete(ws.id, "ghost")


async def test_file_get_specific_missing_version(store: WorkspaceStore) -> None:
    ws = await store.create_workspace("files-ws")
    await store.file_put(ws.id, "x.txt", b"y")
    with pytest.raises(FileNotFound):
        await store.file_read(ws.id, "x.txt", version=99)


async def test_file_list_excludes_tombstones(store: WorkspaceStore) -> None:
    ws = await store.create_workspace("files-ws")
    await store.file_put(ws.id, "alive.txt", b"a")
    await store.file_put(ws.id, "doomed.txt", b"d")
    await store.file_delete(ws.id, "doomed.txt")
    listed = await store.file_list(ws.id)
    assert [f.path for f in listed] == ["alive.txt"]


async def test_file_sha256_dedup(store: WorkspaceStore) -> None:
    ws = await store.create_workspace("dedup")
    e1 = await store.file_put(ws.id, "a.txt", b"same content")
    e2 = await store.file_put(ws.id, "b.txt", b"same content")
    assert e1.sha256 == e2.sha256
    # Each file got its own version-1 row but they share a single blob.
    blob = store._blob_path(ws.id, e1.sha256)
    assert blob.exists()


async def test_file_blob_corruption_yields_404(store: WorkspaceStore) -> None:
    ws = await store.create_workspace("corrupt")
    e = await store.file_put(ws.id, "a.txt", b"original")
    blob = store._blob_path(ws.id, e.sha256)
    blob.write_bytes(b"tampered")
    with pytest.raises(FileNotFound):
        await store.file_read(ws.id, "a.txt")


async def test_file_blob_missing_yields_404(store: WorkspaceStore) -> None:
    ws = await store.create_workspace("rm-blob")
    e = await store.file_put(ws.id, "a.txt", b"x")
    store._blob_path(ws.id, e.sha256).unlink()
    with pytest.raises(FileNotFound):
        await store.file_read(ws.id, "a.txt")
