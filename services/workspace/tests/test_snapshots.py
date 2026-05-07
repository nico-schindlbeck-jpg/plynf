# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Snapshot, branch, diff, and merge tests at the storage layer."""

from __future__ import annotations

import pytest

from plinth_workspace.exceptions import (
    BranchAlreadyMerged,
    BranchNotFound,
    KeyNotFound,
    SnapshotNotFound,
    WorkspaceNotFound,
)
from plinth_workspace.snapshots import SnapshotStore
from plinth_workspace.storage import WorkspaceStore

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Snapshots


async def test_snapshot_captures_latest_versions(
    store: WorkspaceStore, snapshots: SnapshotStore
) -> None:
    ws = await store.create_workspace("snap-ws")
    await store.kv_put(ws.id, "topic", "wind")
    await store.kv_put(ws.id, "topic", "solar")
    await store.kv_put(ws.id, "tags", ["a"])
    await store.file_put(ws.id, "report.md", b"hi")

    snap = await snapshots.create_snapshot(ws.id, "baseline", message="first cut")
    assert snap.id.startswith("snap_")
    assert snap.kv_versions == {"topic": 2, "tags": 1}
    assert snap.file_versions == {"report.md": 1}
    assert snap.message == "first cut"
    assert snap.parent_snapshot_id is None


async def test_snapshot_excludes_tombstones(
    store: WorkspaceStore, snapshots: SnapshotStore
) -> None:
    ws = await store.create_workspace("snap-ws")
    await store.kv_put(ws.id, "alive", 1)
    await store.kv_put(ws.id, "doomed", 2)
    await store.kv_delete(ws.id, "doomed")
    snap = await snapshots.create_snapshot(ws.id, "after-delete")
    assert snap.kv_versions == {"alive": 1}


async def test_list_snapshots(
    store: WorkspaceStore, snapshots: SnapshotStore
) -> None:
    ws = await store.create_workspace("snap-ws")
    await store.kv_put(ws.id, "k", "v")
    s1 = await snapshots.create_snapshot(ws.id, "first")
    await store.kv_put(ws.id, "k", "v2")
    s2 = await snapshots.create_snapshot(ws.id, "second")
    listed = await snapshots.list_snapshots(ws.id)
    ids = [s.id for s in listed]
    assert set(ids) == {s1.id, s2.id}


async def test_get_snapshot_missing(
    store: WorkspaceStore, snapshots: SnapshotStore
) -> None:
    ws = await store.create_workspace("snap-ws")
    with pytest.raises(SnapshotNotFound):
        await snapshots.get_snapshot(ws.id, "snap_nope")


async def test_diff_added_modified_deleted(
    store: WorkspaceStore, snapshots: SnapshotStore
) -> None:
    ws = await store.create_workspace("diff-ws")
    await store.kv_put(ws.id, "kept", "x")
    await store.kv_put(ws.id, "modified", "v1")
    await store.kv_put(ws.id, "to_remove", "x")
    await store.file_put(ws.id, "stable.txt", b"x")
    s_before = await snapshots.create_snapshot(ws.id, "before")

    await store.kv_put(ws.id, "modified", "v2")
    await store.kv_put(ws.id, "added", "y")
    await store.kv_delete(ws.id, "to_remove")
    await store.file_put(ws.id, "stable.txt", b"y")  # modify file
    await store.file_put(ws.id, "new.txt", b"new")
    s_after = await snapshots.create_snapshot(ws.id, "after")

    diff = await snapshots.diff_snapshots(ws.id, s_before.id, s_after.id)
    assert "added" in diff.kv_added
    assert "modified" in diff.kv_modified
    assert "to_remove" in diff.kv_deleted
    assert "stable.txt" in diff.files_modified
    assert "new.txt" in diff.files_added


async def test_diff_missing_snapshot(
    store: WorkspaceStore, snapshots: SnapshotStore
) -> None:
    ws = await store.create_workspace("diff-ws")
    s = await snapshots.create_snapshot(ws.id, "only")
    with pytest.raises(SnapshotNotFound):
        await snapshots.diff_snapshots(ws.id, s.id, "snap_missing")


async def test_create_snapshot_missing_workspace(
    snapshots: SnapshotStore,
) -> None:
    with pytest.raises(WorkspaceNotFound):
        await snapshots.create_snapshot("ws_nope", "x")


# ---------------------------------------------------------------------------
# Branches


async def test_create_branch_from_snapshot(
    store: WorkspaceStore, snapshots: SnapshotStore
) -> None:
    ws = await store.create_workspace("br-ws")
    await store.kv_put(ws.id, "topic", "main-v1")
    snap = await snapshots.create_snapshot(ws.id, "base")
    br = await snapshots.create_branch(ws.id, "experiment", snap.id)
    assert br.id.startswith("br_")
    assert br.from_snapshot_id == snap.id
    assert br.merged is False


async def test_branch_isolated_from_main(
    store: WorkspaceStore, snapshots: SnapshotStore
) -> None:
    ws = await store.create_workspace("br-ws")
    await store.kv_put(ws.id, "topic", "main-v1")
    snap = await snapshots.create_snapshot(ws.id, "base")
    br = await snapshots.create_branch(ws.id, "exp", snap.id)

    # Write on the branch — must not bump main's version.
    await store.kv_put(ws.id, "topic", "branch-v2", branch_id=br.id)

    main_latest = await store.kv_get(ws.id, "topic")
    assert main_latest.value == "main-v1"
    assert main_latest.version == 1

    branch_latest = await store.kv_get(ws.id, "topic", branch_id=br.id)
    assert branch_latest.value == "branch-v2"
    assert branch_latest.version == 1  # first write on the branch


async def test_branch_read_falls_through_to_snapshot(
    store: WorkspaceStore, snapshots: SnapshotStore
) -> None:
    ws = await store.create_workspace("br-ws")
    await store.kv_put(ws.id, "k1", "main-v1")
    await store.kv_put(ws.id, "k2", "main-v1")
    snap = await snapshots.create_snapshot(ws.id, "base")
    # Mutate main *after* snapshot — branch reads should still see the
    # snapshotted value, not the new main value.
    await store.kv_put(ws.id, "k1", "main-v2")
    br = await snapshots.create_branch(ws.id, "ro", snap.id)

    on_branch = await store.kv_get(ws.id, "k1", branch_id=br.id)
    assert on_branch.value == "main-v1"
    assert on_branch.version == 1
    on_branch_k2 = await store.kv_get(ws.id, "k2", branch_id=br.id)
    assert on_branch_k2.value == "main-v1"


async def test_branch_delete_tombstones_inherited_key(
    store: WorkspaceStore, snapshots: SnapshotStore
) -> None:
    ws = await store.create_workspace("br-ws")
    await store.kv_put(ws.id, "topic", "main-v1")
    snap = await snapshots.create_snapshot(ws.id, "base")
    br = await snapshots.create_branch(ws.id, "exp", snap.id)

    # Delete on branch — main is untouched but branch reads must 404.
    await store.kv_delete(ws.id, "topic", branch_id=br.id)
    with pytest.raises(KeyNotFound):
        await store.kv_get(ws.id, "topic", branch_id=br.id)
    on_main = await store.kv_get(ws.id, "topic")
    assert on_main.value == "main-v1"


async def test_branch_kv_list_falls_through(
    store: WorkspaceStore, snapshots: SnapshotStore
) -> None:
    ws = await store.create_workspace("br-ws")
    await store.kv_put(ws.id, "a", 1)
    await store.kv_put(ws.id, "b", 2)
    snap = await snapshots.create_snapshot(ws.id, "base")
    br = await snapshots.create_branch(ws.id, "exp", snap.id)

    await store.kv_put(ws.id, "b", 22, branch_id=br.id)
    await store.kv_put(ws.id, "c", 3, branch_id=br.id)
    listed = await store.kv_list(ws.id, branch_id=br.id)
    assert {(e.key, e.value) for e in listed} == {("a", 1), ("b", 22), ("c", 3)}


async def test_branch_history_combines_main_and_branch(
    store: WorkspaceStore, snapshots: SnapshotStore
) -> None:
    ws = await store.create_workspace("br-ws")
    await store.kv_put(ws.id, "k", "m1")
    await store.kv_put(ws.id, "k", "m2")
    snap = await snapshots.create_snapshot(ws.id, "base")
    # main keeps moving — but branch sees only up to snapshot.
    await store.kv_put(ws.id, "k", "m3")
    br = await snapshots.create_branch(ws.id, "exp", snap.id)
    await store.kv_put(ws.id, "k", "b1", branch_id=br.id)
    history = await store.kv_history(ws.id, "k", branch_id=br.id)
    values = [e.value for e in history]
    assert values == ["m1", "m2", "b1"]


async def test_branch_files(
    store: WorkspaceStore, snapshots: SnapshotStore
) -> None:
    ws = await store.create_workspace("br-ws")
    await store.file_put(ws.id, "f.txt", b"main-1")
    snap = await snapshots.create_snapshot(ws.id, "base")
    br = await snapshots.create_branch(ws.id, "exp", snap.id)

    await store.file_put(ws.id, "f.txt", b"branch-1", branch_id=br.id)
    _, data = await store.file_read(ws.id, "f.txt", branch_id=br.id)
    assert data == b"branch-1"
    _, main_data = await store.file_read(ws.id, "f.txt")
    assert main_data == b"main-1"


async def test_branch_create_missing_snapshot(
    store: WorkspaceStore, snapshots: SnapshotStore
) -> None:
    ws = await store.create_workspace("br-ws")
    with pytest.raises(SnapshotNotFound):
        await snapshots.create_branch(ws.id, "x", "snap_nope")


async def test_branch_get_and_list(
    store: WorkspaceStore, snapshots: SnapshotStore
) -> None:
    ws = await store.create_workspace("br-ws")
    snap = await snapshots.create_snapshot(ws.id, "base")
    br = await snapshots.create_branch(ws.id, "exp", snap.id)
    listed = await snapshots.list_branches(ws.id)
    assert [b.id for b in listed] == [br.id]
    fetched = await snapshots.get_branch(ws.id, br.id)
    assert fetched.id == br.id


async def test_branch_get_missing(
    store: WorkspaceStore, snapshots: SnapshotStore
) -> None:
    ws = await store.create_workspace("br-ws")
    with pytest.raises(BranchNotFound):
        await snapshots.get_branch(ws.id, "br_nope")


async def test_branch_delete(
    store: WorkspaceStore, snapshots: SnapshotStore
) -> None:
    ws = await store.create_workspace("br-ws")
    snap = await snapshots.create_snapshot(ws.id, "base")
    br = await snapshots.create_branch(ws.id, "exp", snap.id)
    await store.kv_put(ws.id, "k", "v", branch_id=br.id)
    await snapshots.delete_branch(ws.id, br.id)
    with pytest.raises(BranchNotFound):
        await snapshots.get_branch(ws.id, br.id)
    # Branch-attached entries are gone, but workspace and main entries
    # are untouched.


async def test_branch_delete_missing(
    store: WorkspaceStore, snapshots: SnapshotStore
) -> None:
    ws = await store.create_workspace("br-ws")
    with pytest.raises(BranchNotFound):
        await snapshots.delete_branch(ws.id, "br_nope")


# ---------------------------------------------------------------------------
# Merge


async def test_merge_branch_into_main(
    store: WorkspaceStore, snapshots: SnapshotStore
) -> None:
    ws = await store.create_workspace("merge-ws")
    await store.kv_put(ws.id, "topic", "main-v1")
    snap = await snapshots.create_snapshot(ws.id, "base")
    br = await snapshots.create_branch(ws.id, "exp", snap.id)
    await store.kv_put(ws.id, "topic", "branch-v1", branch_id=br.id)
    await store.file_put(ws.id, "out.txt", b"branch-out", branch_id=br.id)

    result = await snapshots.merge_branch(ws.id, br.id)
    assert "topic" in result.kv_merged
    assert "out.txt" in result.files_merged

    # On main now carries the branch's values as a new version.
    main_topic = await store.kv_get(ws.id, "topic")
    assert main_topic.value == "branch-v1"
    assert main_topic.version == 2

    _, main_file = await store.file_read(ws.id, "out.txt")
    assert main_file == b"branch-out"

    branch = await snapshots.get_branch(ws.id, br.id)
    assert branch.merged is True
    assert branch.merged_at is not None


async def test_merge_branch_propagates_deletions(
    store: WorkspaceStore, snapshots: SnapshotStore
) -> None:
    ws = await store.create_workspace("merge-del")
    await store.kv_put(ws.id, "topic", "v1")
    snap = await snapshots.create_snapshot(ws.id, "base")
    br = await snapshots.create_branch(ws.id, "exp", snap.id)
    await store.kv_delete(ws.id, "topic", branch_id=br.id)

    await snapshots.merge_branch(ws.id, br.id)
    with pytest.raises(KeyNotFound):
        await store.kv_get(ws.id, "topic")


async def test_merge_branch_already_merged(
    store: WorkspaceStore, snapshots: SnapshotStore
) -> None:
    ws = await store.create_workspace("merge-twice")
    snap = await snapshots.create_snapshot(ws.id, "base")
    br = await snapshots.create_branch(ws.id, "exp", snap.id)
    await store.kv_put(ws.id, "k", "v", branch_id=br.id)
    await snapshots.merge_branch(ws.id, br.id)
    with pytest.raises(BranchAlreadyMerged):
        await snapshots.merge_branch(ws.id, br.id)


async def test_merge_missing_branch(
    store: WorkspaceStore, snapshots: SnapshotStore
) -> None:
    ws = await store.create_workspace("merge-miss")
    with pytest.raises(BranchNotFound):
        await snapshots.merge_branch(ws.id, "br_nope")


async def test_snapshot_on_branch_records_parent(
    store: WorkspaceStore, snapshots: SnapshotStore
) -> None:
    ws = await store.create_workspace("snap-on-br")
    await store.kv_put(ws.id, "k", 1)
    s1 = await snapshots.create_snapshot(ws.id, "base")
    br = await snapshots.create_branch(ws.id, "exp", s1.id)
    await store.kv_put(ws.id, "k", 2, branch_id=br.id)
    s2 = await snapshots.create_snapshot(ws.id, "on-branch", branch_id=br.id)
    assert s2.parent_snapshot_id == s1.id
    assert s2.kv_versions["k"] == 1  # version per branch is its own counter


async def test_snapshot_on_branch_captures_files(
    store: WorkspaceStore, snapshots: SnapshotStore
) -> None:
    """Branch snapshot picks up branch-overridden, branch-new, and inherited files."""

    ws = await store.create_workspace("snap-files-br")
    await store.file_put(ws.id, "main-only.txt", b"main")
    await store.file_put(ws.id, "shared.txt", b"main-v1")
    s1 = await snapshots.create_snapshot(ws.id, "base")
    br = await snapshots.create_branch(ws.id, "exp", s1.id)

    await store.file_put(ws.id, "shared.txt", b"branch-v1", branch_id=br.id)
    await store.file_put(ws.id, "branch-only.txt", b"x", branch_id=br.id)

    s2 = await snapshots.create_snapshot(ws.id, "on-branch", branch_id=br.id)
    # main-only flows through from the branch's snapshot capture
    assert s2.file_versions["main-only.txt"] == 1
    # shared was overridden on the branch
    assert s2.file_versions["shared.txt"] == 1  # first write on branch
    # branch-only only exists on the branch
    assert s2.file_versions["branch-only.txt"] == 1


async def test_snapshot_on_branch_drops_deleted_files(
    store: WorkspaceStore, snapshots: SnapshotStore
) -> None:
    ws = await store.create_workspace("snap-files-del")
    await store.file_put(ws.id, "doomed.txt", b"goodbye")
    s1 = await snapshots.create_snapshot(ws.id, "base")
    br = await snapshots.create_branch(ws.id, "exp", s1.id)
    await store.file_delete(ws.id, "doomed.txt", branch_id=br.id)
    s2 = await snapshots.create_snapshot(ws.id, "post-delete", branch_id=br.id)
    assert "doomed.txt" not in s2.file_versions


async def test_branch_file_list_falls_through(
    store: WorkspaceStore, snapshots: SnapshotStore
) -> None:
    ws = await store.create_workspace("br-flist")
    await store.file_put(ws.id, "a.txt", b"a")
    await store.file_put(ws.id, "b.txt", b"b")
    snap = await snapshots.create_snapshot(ws.id, "base")
    br = await snapshots.create_branch(ws.id, "exp", snap.id)
    await store.file_put(ws.id, "b.txt", b"b2", branch_id=br.id)
    await store.file_put(ws.id, "c.txt", b"c", branch_id=br.id)
    listed = await store.file_list(ws.id, branch_id=br.id)
    paths = {f.path for f in listed}
    assert paths == {"a.txt", "b.txt", "c.txt"}


async def test_branch_file_specific_version_falls_through(
    store: WorkspaceStore, snapshots: SnapshotStore
) -> None:
    ws = await store.create_workspace("br-fv")
    await store.file_put(ws.id, "a.txt", b"a-v1")
    await store.file_put(ws.id, "a.txt", b"a-v2")
    snap = await snapshots.create_snapshot(ws.id, "base")
    br = await snapshots.create_branch(ws.id, "exp", snap.id)
    # No write on the branch — specific version 1 must fall through to main.
    _, data = await store.file_read(ws.id, "a.txt", version=1, branch_id=br.id)
    assert data == b"a-v1"


async def test_branch_kv_specific_version_falls_through(
    store: WorkspaceStore, snapshots: SnapshotStore
) -> None:
    ws = await store.create_workspace("br-kv-fv")
    await store.kv_put(ws.id, "k", "v1")
    await store.kv_put(ws.id, "k", "v2")
    snap = await snapshots.create_snapshot(ws.id, "base")
    br = await snapshots.create_branch(ws.id, "exp", snap.id)
    fetched = await store.kv_get(ws.id, "k", version=1, branch_id=br.id)
    assert fetched.value == "v1"
