# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for ``plinth.workspace`` — KV, files, snapshots, branches."""

from __future__ import annotations

import httpx
import pytest
import respx

from plinth import (
    BranchNotFound,
    FileNotFound,
    KeyNotFound,
    Plinth,
    SnapshotNotFound,
    Workspace,
)

from .conftest import (
    error_envelope,
    make_branch,
    make_file_entry,
    make_kv_entry,
    make_snapshot,
    make_workspace,
)

# ---------------------------------------------------------------------------
# Helper — produce a ready-to-use workspace handle without needing a real
# get-or-create round-trip in every test.
# ---------------------------------------------------------------------------


@pytest.fixture
def ws(client: Plinth, workspace_mock: respx.MockRouter) -> Workspace:
    """Return a Workspace bound to ws_01TESTWORKSPACE."""
    workspace_mock.get("/v1/workspaces").mock(
        return_value=httpx.Response(200, json={"workspaces": [make_workspace()]})
    )
    return client.workspace("research-task-1")


# ---------------------------------------------------------------------------
# KV — set / get / history / delete / list
# ---------------------------------------------------------------------------


def test_kv_set_returns_kventry(ws: Workspace, workspace_mock: respx.MockRouter):
    payload = make_kv_entry(key="topic", value="renewable energy", version=1)
    route = workspace_mock.put(f"/v1/workspaces/{ws.id}/kv/topic").mock(
        return_value=httpx.Response(200, json=payload)
    )

    entry = ws.kv.set("topic", "renewable energy")

    assert entry.value == "renewable energy"
    assert entry.version == 1
    assert route.called
    body = route.calls.last.request.read()
    assert b"renewable energy" in body


def test_kv_get_returns_value_by_default(ws: Workspace, workspace_mock: respx.MockRouter):
    workspace_mock.get(f"/v1/workspaces/{ws.id}/kv/topic").mock(
        return_value=httpx.Response(
            200,
            json=make_kv_entry(key="topic", value="renewable energy", version=2),
        )
    )

    assert ws.kv.get("topic") == "renewable energy"


def test_kv_get_with_version_returns_tuple(ws: Workspace, workspace_mock: respx.MockRouter):
    workspace_mock.get(f"/v1/workspaces/{ws.id}/kv/topic").mock(
        return_value=httpx.Response(200, json=make_kv_entry(key="topic", value="x", version=7))
    )

    value, version = ws.kv.get("topic", with_version=True)

    assert value == "x"
    assert version == 7


def test_kv_get_with_meta_returns_kventry(ws: Workspace, workspace_mock: respx.MockRouter):
    workspace_mock.get(f"/v1/workspaces/{ws.id}/kv/topic").mock(
        return_value=httpx.Response(200, json=make_kv_entry(key="topic", value="x", version=3))
    )

    entry = ws.kv.get("topic", with_meta=True)

    assert entry.version == 3
    assert entry.key == "topic"


def test_kv_get_specific_version(ws: Workspace, workspace_mock: respx.MockRouter):
    captured: dict = {}

    def handler(request):
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json=make_kv_entry(key="topic", value="old", version=3))

    workspace_mock.get(f"/v1/workspaces/{ws.id}/kv/topic").mock(side_effect=handler)
    val = ws.kv.get("topic", version=3)

    assert val == "old"
    assert captured["params"] == {"version": "3"}


def test_kv_get_404_raises_keynotfound(ws: Workspace, workspace_mock: respx.MockRouter):
    workspace_mock.get(f"/v1/workspaces/{ws.id}/kv/missing").mock(
        return_value=httpx.Response(404, json=error_envelope("KEY_NOT_FOUND", "no such key"))
    )

    with pytest.raises(KeyNotFound):
        ws.kv.get("missing")


def test_kv_get_with_default_swallows_keynotfound(ws: Workspace, workspace_mock: respx.MockRouter):
    workspace_mock.get(f"/v1/workspaces/{ws.id}/kv/missing").mock(
        return_value=httpx.Response(404, json=error_envelope("KEY_NOT_FOUND", "no such key"))
    )

    out = ws.kv.get("missing", default="fallback")

    assert out == "fallback"


def test_kv_history_returns_list_of_entries(ws: Workspace, workspace_mock: respx.MockRouter):
    versions = [
        make_kv_entry(key="topic", value="v1", version=1),
        make_kv_entry(key="topic", value="v2", version=2),
    ]
    workspace_mock.get(f"/v1/workspaces/{ws.id}/kv/topic/history").mock(
        return_value=httpx.Response(200, json={"versions": versions})
    )

    history = ws.kv.history("topic")

    assert [e.version for e in history] == [1, 2]


def test_kv_delete(ws: Workspace, workspace_mock: respx.MockRouter):
    route = workspace_mock.delete(f"/v1/workspaces/{ws.id}/kv/topic").mock(
        return_value=httpx.Response(204)
    )

    ws.kv.delete("topic")

    assert route.called


def test_kv_list(ws: Workspace, workspace_mock: respx.MockRouter):
    entries = [
        make_kv_entry(key="a", value=1, version=1),
        make_kv_entry(key="b", value=2, version=3),
    ]
    workspace_mock.get(f"/v1/workspaces/{ws.id}/kv").mock(
        return_value=httpx.Response(200, json={"entries": entries})
    )

    out = ws.kv.list()

    assert {e.key for e in out} == {"a", "b"}


# ---------------------------------------------------------------------------
# Files — write/read text + binary, meta, list, delete.
# ---------------------------------------------------------------------------


def test_files_write_text_sends_utf8_body(ws: Workspace, workspace_mock: respx.MockRouter):
    captured: dict = {}

    def handler(request):
        captured["body"] = request.read()
        captured["content_type"] = request.headers.get("content-type")
        return httpx.Response(
            200,
            json=make_file_entry(path="report.md", size=len(captured["body"])),
        )

    workspace_mock.put(f"/v1/workspaces/{ws.id}/files/report.md").mock(side_effect=handler)
    entry = ws.files.write("report.md", "# Report\n...")

    assert entry.path == "report.md"
    assert captured["body"] == b"# Report\n..."
    assert captured["content_type"].startswith("text/plain")


def test_files_write_binary_with_explicit_content_type(
    ws: Workspace, workspace_mock: respx.MockRouter
):
    captured: dict = {}

    def handler(request):
        captured["body"] = request.read()
        captured["content_type"] = request.headers.get("content-type")
        return httpx.Response(
            200,
            json=make_file_entry(
                path="data.bin",
                content_type="application/octet-stream",
                size=4,
            ),
        )

    workspace_mock.put(f"/v1/workspaces/{ws.id}/files/data.bin").mock(side_effect=handler)
    ws.files.write("data.bin", b"\x00\x01\x02\x03", content_type="application/octet-stream")

    assert captured["body"] == b"\x00\x01\x02\x03"
    assert captured["content_type"] == "application/octet-stream"


def test_files_read_returns_bytes(ws: Workspace, workspace_mock: respx.MockRouter):
    workspace_mock.get(f"/v1/workspaces/{ws.id}/files/report.md").mock(
        return_value=httpx.Response(200, content=b"hello")
    )

    assert ws.files.read("report.md") == b"hello"


def test_files_read_as_text(ws: Workspace, workspace_mock: respx.MockRouter):
    workspace_mock.get(f"/v1/workspaces/{ws.id}/files/report.md").mock(
        return_value=httpx.Response(200, content=b"hello")
    )

    assert ws.files.read("report.md", as_text=True) == "hello"


def test_files_read_404_raises_filenotfound(ws: Workspace, workspace_mock: respx.MockRouter):
    workspace_mock.get(f"/v1/workspaces/{ws.id}/files/missing").mock(
        return_value=httpx.Response(404, json=error_envelope("FILE_NOT_FOUND", "missing"))
    )

    with pytest.raises(FileNotFound):
        ws.files.read("missing")


def test_files_meta(ws: Workspace, workspace_mock: respx.MockRouter):
    workspace_mock.get(f"/v1/workspaces/{ws.id}/files/report.md/meta").mock(
        return_value=httpx.Response(200, json=make_file_entry(path="report.md", size=99))
    )

    meta = ws.files.meta("report.md")

    assert meta.size == 99


def test_files_list(ws: Workspace, workspace_mock: respx.MockRouter):
    files = [
        make_file_entry(path="a.txt"),
        make_file_entry(path="b.txt"),
    ]
    workspace_mock.get(f"/v1/workspaces/{ws.id}/files").mock(
        return_value=httpx.Response(200, json={"files": files})
    )

    out = ws.files.list()

    assert {f.path for f in out} == {"a.txt", "b.txt"}


def test_files_delete(ws: Workspace, workspace_mock: respx.MockRouter):
    route = workspace_mock.delete(f"/v1/workspaces/{ws.id}/files/old.md").mock(
        return_value=httpx.Response(204)
    )

    ws.files.delete("old.md")

    assert route.called


# ---------------------------------------------------------------------------
# Snapshots & branches
# ---------------------------------------------------------------------------


def test_snapshot_creates(ws: Workspace, workspace_mock: respx.MockRouter):
    snap = make_snapshot(name="baseline", message="initial state")
    workspace_mock.post(f"/v1/workspaces/{ws.id}/snapshots").mock(
        return_value=httpx.Response(201, json=snap)
    )

    out = ws.snapshot("baseline", message="initial state")

    assert out.id == snap["id"]
    assert out.name == "baseline"


def test_snapshots_list(ws: Workspace, workspace_mock: respx.MockRouter):
    workspace_mock.get(f"/v1/workspaces/{ws.id}/snapshots").mock(
        return_value=httpx.Response(
            200,
            json={
                "snapshots": [
                    make_snapshot(snap_id="snap_A", name="a"),
                    make_snapshot(snap_id="snap_B", name="b"),
                ]
            },
        )
    )

    out = ws.snapshots()

    assert {s.id for s in out} == {"snap_A", "snap_B"}


def test_diff_two_snapshots(ws: Workspace, workspace_mock: respx.MockRouter):
    captured: dict = {}

    def handler(request):
        captured["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "kv_added": ["k1"],
                "kv_modified": [],
                "kv_deleted": [],
                "files_added": [],
                "files_modified": [],
                "files_deleted": [],
            },
        )

    workspace_mock.get(f"/v1/workspaces/{ws.id}/snapshots/snap_A/diff").mock(side_effect=handler)
    diff = ws.diff("snap_A", "snap_B")

    assert diff.kv_added == ["k1"]
    assert captured["params"] == {"against": "snap_B"}


def test_diff_404_raises_snapshotnotfound(ws: Workspace, workspace_mock: respx.MockRouter):
    workspace_mock.get(f"/v1/workspaces/{ws.id}/snapshots/nope/diff").mock(
        return_value=httpx.Response(404, json=error_envelope("SNAPSHOT_NOT_FOUND", "no"))
    )

    with pytest.raises(SnapshotNotFound):
        ws.diff("nope", "snap_B")


def test_branch_create(ws: Workspace, workspace_mock: respx.MockRouter):
    captured: dict = {}

    def handler(request):
        captured["body"] = request.read()
        return httpx.Response(201, json=make_branch(name="experiment", from_snapshot_id="snap_A"))

    workspace_mock.post(f"/v1/workspaces/{ws.id}/branches").mock(side_effect=handler)
    branch = ws.branch("experiment", from_snapshot="snap_A")

    assert branch.name == "experiment"
    assert branch.from_snapshot_id == "snap_A"
    assert b"snap_A" in captured["body"]


def test_branches_list(ws: Workspace, workspace_mock: respx.MockRouter):
    workspace_mock.get(f"/v1/workspaces/{ws.id}/branches").mock(
        return_value=httpx.Response(200, json={"branches": [make_branch(branch_id="br_X")]})
    )

    out = ws.branches()

    assert out[0].id == "br_X"


def test_merge_returns_merge_result(ws: Workspace, workspace_mock: respx.MockRouter):
    workspace_mock.post(f"/v1/workspaces/{ws.id}/branches/br_X/merge").mock(
        return_value=httpx.Response(
            200,
            json={
                "branch_id": "br_X",
                "workspace_id": ws.id,
                "merged_at": "2030-01-01T00:00:00+00:00",
                "kv_keys_merged": ["topic"],
                "file_paths_merged": [],
                "conflicts": [],
            },
        )
    )

    out = ws.merge("br_X")

    assert out.branch_id == "br_X"
    assert "topic" in out.kv_keys_merged


def test_merge_404_raises_branchnotfound(ws: Workspace, workspace_mock: respx.MockRouter):
    workspace_mock.post(f"/v1/workspaces/{ws.id}/branches/nope/merge").mock(
        return_value=httpx.Response(404, json=error_envelope("BRANCH_NOT_FOUND", "no such branch"))
    )

    with pytest.raises(BranchNotFound):
        ws.merge("nope")


def test_delete_branch(ws: Workspace, workspace_mock: respx.MockRouter):
    route = workspace_mock.delete(f"/v1/workspaces/{ws.id}/branches/br_X").mock(
        return_value=httpx.Response(204)
    )

    ws.delete_branch("br_X")

    assert route.called


# ---------------------------------------------------------------------------
# with_branch — verifies isolation
# ---------------------------------------------------------------------------


def test_with_branch_appends_branch_query(ws: Workspace, workspace_mock: respx.MockRouter):
    captured_main: dict = {}
    captured_branch: dict = {}

    def main_handler(request):
        captured_main["params"] = dict(request.url.params)
        return httpx.Response(200, json=make_kv_entry(key="topic", value="main", version=1))

    def branch_handler(request):
        captured_branch["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json=make_kv_entry(key="topic", value="branch", version=2, branch_id="br_X"),
        )

    # The same URL is used for both calls; respx matches in declaration order.
    workspace_mock.put(f"/v1/workspaces/{ws.id}/kv/topic").mock(
        side_effect=[
            httpx.Response(200, json=make_kv_entry(key="topic", value="main", version=1)),
            httpx.Response(
                200,
                json=make_kv_entry(key="topic", value="branch", version=2, branch_id="br_X"),
            ),
        ]
    )

    ws.kv.set("topic", "main")
    branch_view = ws.with_branch("br_X")
    branch_view.kv.set("topic", "branch")

    # The second request must include ?branch=br_X.
    requests = workspace_mock.calls
    second_request = requests[-1].request
    assert second_request.url.params.get("branch") == "br_X"
    # And the first did not.
    first_request = requests[-2].request
    assert "branch" not in first_request.url.params


def test_with_branch_returns_distinct_object(ws: Workspace):
    branch_view = ws.with_branch("br_X")
    assert branch_view is not ws
    assert branch_view.branch_id == "br_X"
    assert ws.branch_id is None
    # Same workspace ID though.
    assert branch_view.id == ws.id


def test_workspace_repr_does_not_crash(ws: Workspace):
    # Just exercising __repr__ for coverage.
    assert ws.id in repr(ws)


def test_workspace_model_property_returns_pydantic(ws: Workspace):
    from plinth.models import Workspace as WorkspaceModel

    assert isinstance(ws.model, WorkspaceModel)
    assert ws.model.id == ws.id


def test_files_read_specific_version(ws: Workspace, workspace_mock: respx.MockRouter):
    captured: dict = {}

    def handler(request):
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, content=b"old version")

    workspace_mock.get(f"/v1/workspaces/{ws.id}/files/report.md").mock(side_effect=handler)
    body = ws.files.read("report.md", version=2)
    assert body == b"old version"
    assert captured["params"]["version"] == "2"


def test_snapshot_get_by_id(ws: Workspace, workspace_mock: respx.MockRouter):
    workspace_mock.get(f"/v1/workspaces/{ws.id}/snapshots/snap_X").mock(
        return_value=httpx.Response(200, json=make_snapshot(snap_id="snap_X"))
    )
    out = ws._snapshots.get("snap_X")
    assert out.id == "snap_X"


def test_snapshot_get_404(ws: Workspace, workspace_mock: respx.MockRouter):
    workspace_mock.get(f"/v1/workspaces/{ws.id}/snapshots/nope").mock(
        return_value=httpx.Response(404, json=error_envelope("SNAPSHOT_NOT_FOUND", "no"))
    )
    with pytest.raises(SnapshotNotFound):
        ws._snapshots.get("nope")
