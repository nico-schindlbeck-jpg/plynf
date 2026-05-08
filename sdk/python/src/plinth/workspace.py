# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Workspace client and its KV / Files / Snapshot proxies.

A :class:`Workspace` wraps a single workspace ID and provides the four
sub-namespaces an agent uses day-to-day:

* ``ws.kv``        — versioned key-value store
* ``ws.files``     — versioned blob storage
* ``ws.snapshot``  — point-in-time snapshots
* ``ws.branch``    — divergent timelines forked from a snapshot

The proxies are intentionally thin: they translate Pythonic calls into
HTTP requests and parse the responses back into the Pydantic models
defined in :mod:`plinth.models`. All HTTP errors are mapped to typed
exceptions by :class:`plinth._http.HTTPClient`.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Iterator, overload
from urllib.parse import quote

from ._http import HTTPClient
from .exceptions import (
    BranchNotFound,
    FileNotFound,
    KeyNotFound,
    LockNotFound,
    SnapshotNotFound,
    WorkspaceNotFound,
)
from .models import (
    Branch,
    DiffResult,
    FileEntry,
    KVEntry,
    Lock,
    MergeResult,
    Snapshot,
)
from .models import (
    Workspace as WorkspaceModel,
)

if TYPE_CHECKING:
    from .channels import ChannelsProxy
    from .workflows import WorkflowsProxy


def _ek(key: str) -> str:
    """Percent-encode a KV key (slashes etc.) for safe URL embedding."""
    return quote(key, safe="")


def _ep(path: str) -> str:
    """Percent-encode a file path, preserving slashes between segments."""
    return quote(path, safe="/")


def _en(name: str) -> str:
    """Percent-encode a lock name.

    Lock names use the workspace service's ``{name:path}`` route so the
    canonical ``/``-prefixed style (e.g. ``kv:sources/index``) is
    preserved unescaped. Other delimiters are still escaped so the URL
    parses correctly.
    """
    return quote(name, safe="/:")

# ---------------------------------------------------------------------------
# Sentinel for "argument not supplied" — we need to distinguish ``None``
# (which is a valid KV value) from "caller did not pass this kwarg".
# ---------------------------------------------------------------------------


class _Missing:
    """Sentinel for missing keyword arguments."""

    def __repr__(self) -> str:  # pragma: no cover - cosmetic only
        return "<MISSING>"


_MISSING: Any = _Missing()


# ---------------------------------------------------------------------------
# KV proxy
# ---------------------------------------------------------------------------


class KVProxy:
    """Versioned key-value store for a workspace.

    Every :meth:`set` writes a new immutable version. Reads default to
    the latest version; pass ``version=N`` for a specific revision.
    """

    def __init__(self, workspace: Workspace) -> None:
        self._ws = workspace

    # -- writes --------------------------------------------------------

    def set(self, key: str, value: Any) -> KVEntry:
        """Write ``value`` to ``key``, returning the new ``KVEntry``."""
        response = self._ws._http.put(
            f"/v1/workspaces/{self._ws.id}/kv/{_ek(key)}",
            json={"value": value},
            params=self._ws._branch_params(),
            not_found_class=WorkspaceNotFound,
        )
        return KVEntry.model_validate(response.json())

    def delete(self, key: str) -> None:
        """Delete ``key`` (creates a tombstone version)."""
        self._ws._http.delete(
            f"/v1/workspaces/{self._ws.id}/kv/{_ek(key)}",
            params=self._ws._branch_params(),
            not_found_class=KeyNotFound,
        )

    # -- reads ---------------------------------------------------------

    @overload
    def get(self, key: str) -> Any: ...

    @overload
    def get(self, key: str, *, version: int) -> Any: ...

    @overload
    def get(self, key: str, *, with_version: bool) -> tuple[Any, int]: ...

    @overload
    def get(self, key: str, *, with_meta: bool) -> KVEntry: ...

    def get(
        self,
        key: str,
        *,
        version: int | None = None,
        with_version: bool = False,
        with_meta: bool = False,
        default: Any = _MISSING,
    ) -> Any:
        """Read a value from the KV store.

        Args:
            key: The KV key.
            version: Specific version to fetch. If omitted, returns the
                latest.
            with_version: When ``True``, returns ``(value, version)``.
            with_meta: When ``True``, returns the full :class:`KVEntry`.
            default: If supplied, this value is returned instead of
                raising :class:`KeyNotFound` when the key does not exist.

        Returns:
            By default, the raw decoded value. See the keyword arguments
            for alternative return shapes.

        Raises:
            KeyNotFound: If the key does not exist and no ``default``
                was supplied.
        """
        params = self._ws._branch_params()
        if version is not None:
            params["version"] = version
        try:
            response = self._ws._http.get(
                f"/v1/workspaces/{self._ws.id}/kv/{_ek(key)}",
                params=params,
                not_found_class=KeyNotFound,
            )
        except KeyNotFound:
            if default is not _MISSING:
                return default
            raise

        entry = KVEntry.model_validate(response.json())
        if with_meta:
            return entry
        if with_version:
            return entry.value, entry.version
        return entry.value

    def history(self, key: str) -> list[KVEntry]:
        """Return every recorded version of ``key`` (oldest first)."""
        data = self._ws._http.get_json(
            f"/v1/workspaces/{self._ws.id}/kv/{_ek(key)}/history",
            params=self._ws._branch_params(),
            not_found_class=KeyNotFound,
        )
        return [KVEntry.model_validate(v) for v in data.get("versions", [])]

    def list(self) -> list[KVEntry]:
        """List the latest version of every key in the workspace."""
        data = self._ws._http.get_json(
            f"/v1/workspaces/{self._ws.id}/kv",
            params=self._ws._branch_params(),
            not_found_class=WorkspaceNotFound,
        )
        return [KVEntry.model_validate(v) for v in data.get("entries", [])]


# ---------------------------------------------------------------------------
# Files proxy
# ---------------------------------------------------------------------------


class FilesProxy:
    """Versioned blob storage for a workspace."""

    DEFAULT_TEXT_CONTENT_TYPE = "text/plain; charset=utf-8"
    DEFAULT_BINARY_CONTENT_TYPE = "application/octet-stream"

    def __init__(self, workspace: Workspace) -> None:
        self._ws = workspace

    # -- writes --------------------------------------------------------

    def write(
        self,
        path: str,
        content: str | bytes,
        *,
        content_type: str | None = None,
    ) -> FileEntry:
        """Write ``content`` to ``path``, returning a ``FileEntry``.

        Args:
            path: Destination path inside the workspace.
            content: Either ``str`` or ``bytes``.
            content_type: Override the auto-detected MIME type.
        """
        if isinstance(content, str):
            body = content.encode("utf-8")
            ctype = content_type or self.DEFAULT_TEXT_CONTENT_TYPE
        else:
            body = content
            ctype = content_type or self.DEFAULT_BINARY_CONTENT_TYPE

        response = self._ws._http.put(
            f"/v1/workspaces/{self._ws.id}/files/{_ep(path)}",
            content=body,
            params=self._ws._branch_params(),
            headers={"Content-Type": ctype},
            not_found_class=WorkspaceNotFound,
        )
        return FileEntry.model_validate(response.json())

    def delete(self, path: str) -> None:
        """Delete the file at ``path``."""
        self._ws._http.delete(
            f"/v1/workspaces/{self._ws.id}/files/{_ep(path)}",
            params=self._ws._branch_params(),
            not_found_class=FileNotFound,
        )

    # -- reads ---------------------------------------------------------

    @overload
    def read(self, path: str) -> bytes: ...

    @overload
    def read(self, path: str, *, as_text: bool) -> str: ...

    def read(
        self,
        path: str,
        *,
        version: int | None = None,
        as_text: bool = False,
        encoding: str = "utf-8",
    ) -> bytes | str:
        """Read a file's bytes (or text, with ``as_text=True``)."""
        params = self._ws._branch_params()
        if version is not None:
            params["version"] = version
        response = self._ws._http.get(
            f"/v1/workspaces/{self._ws.id}/files/{_ep(path)}",
            params=params,
            not_found_class=FileNotFound,
        )
        if as_text:
            return response.content.decode(encoding)
        return response.content

    def meta(self, path: str) -> FileEntry:
        """Return metadata about ``path`` without downloading bytes."""
        data = self._ws._http.get_json(
            f"/v1/workspaces/{self._ws.id}/files/{_ep(path)}/meta",
            params=self._ws._branch_params(),
            not_found_class=FileNotFound,
        )
        return FileEntry.model_validate(data)

    def list(self) -> list[FileEntry]:
        """List metadata for every file in the workspace."""
        data = self._ws._http.get_json(
            f"/v1/workspaces/{self._ws.id}/files",
            params=self._ws._branch_params(),
            not_found_class=WorkspaceNotFound,
        )
        return [FileEntry.model_validate(f) for f in data.get("files", [])]


# ---------------------------------------------------------------------------
# Snapshot proxy — exposed via ws.snapshot(...) / ws.snapshots() etc.
# ---------------------------------------------------------------------------


class SnapshotProxy:
    """Helper exposing snapshot operations.

    A :class:`Workspace` instance keeps one of these alive so callers
    can reach for it directly when they want — for example,
    ``ws._snapshots.create("foo")`` is equivalent to
    ``ws.snapshot("foo")``.
    """

    def __init__(self, workspace: Workspace) -> None:
        self._ws = workspace

    def create(self, name: str, *, message: str | None = None) -> Snapshot:
        """Create a new snapshot of the current workspace state."""
        body: dict[str, Any] = {"name": name}
        if message is not None:
            body["message"] = message
        response = self._ws._http.post(
            f"/v1/workspaces/{self._ws.id}/snapshots",
            json=body,
            params=self._ws._branch_params(),
            not_found_class=WorkspaceNotFound,
        )
        return Snapshot.model_validate(response.json())

    def list(self) -> list[Snapshot]:
        """Return every snapshot in the workspace, newest last."""
        data = self._ws._http.get_json(
            f"/v1/workspaces/{self._ws.id}/snapshots",
            params=self._ws._branch_params(),
            not_found_class=WorkspaceNotFound,
        )
        return [Snapshot.model_validate(s) for s in data.get("snapshots", [])]

    def get(self, snapshot_id: str) -> Snapshot:
        """Fetch a single snapshot by ID."""
        data = self._ws._http.get_json(
            f"/v1/workspaces/{self._ws.id}/snapshots/{snapshot_id}",
            not_found_class=SnapshotNotFound,
        )
        return Snapshot.model_validate(data)

    def diff(self, a: str, b: str) -> DiffResult:
        """Diff two snapshots (``a`` vs ``b``)."""
        data = self._ws._http.get_json(
            f"/v1/workspaces/{self._ws.id}/snapshots/{a}/diff",
            params={"against": b},
            not_found_class=SnapshotNotFound,
        )
        return DiffResult.model_validate(data)


# ---------------------------------------------------------------------------
# Locks proxy — generic distributed lock primitive (v0.6)
# ---------------------------------------------------------------------------


class LocksProxy:
    """Generic distributed locks over named workspace resources.

    Locks are independent of the workflow-step lease primitive: they
    coordinate any named object (KV key, file path, external resource
    handle) so two agents don't step on each other.

    Typical use::

        # context-manager: acquire + auto-heartbeat + release
        with ws.locks.held("kv:sources/index", holder="agent-A"):
            ws.kv.set("sources/index", new_value)

        # low-level: fully manual
        lock = ws.locks.acquire("name", holder="A", ttl_seconds=30)
        try:
            ...
        finally:
            ws.locks.release("name", holder="A")
    """

    def __init__(self, workspace: Workspace) -> None:
        self._ws = workspace

    # -- low-level API -------------------------------------------------

    def acquire(
        self,
        name: str,
        *,
        holder: str,
        ttl_seconds: int = 60,
        wait_ms: int = 0,
    ) -> Lock:
        """Acquire a lock on ``name``.

        Args:
            name: The named resource to lock. May contain ``/`` (the
                canonical case for KV-/file-prefixed lock names like
                ``kv:sources/index``).
            holder: Caller-chosen holder identifier. Heartbeat and
                release require the same string.
            ttl_seconds: How long the lock survives without a heartbeat.
            wait_ms: ``0`` is fail-fast (raises :class:`LockConflict`
                immediately on contention). Positive values poll the
                server every 100ms until the budget elapses, then raise.

        Returns:
            The persisted :class:`Lock` on success.

        Raises:
            LockConflict: The lock is held by another holder and either
                ``wait_ms`` was zero or expired before it became free.
        """
        response = self._ws._http.post(
            f"/v1/workspaces/{self._ws.id}/locks/{_en(name)}/acquire",
            json={
                "holder": holder,
                "ttl_seconds": ttl_seconds,
                "wait_ms": wait_ms,
            },
            not_found_class=WorkspaceNotFound,
        )
        return Lock.model_validate(response.json())

    def heartbeat(
        self,
        name: str,
        *,
        holder: str,
        ttl_seconds: int | None = None,
    ) -> Lock:
        """Extend the lock's TTL.

        Only the current holder may heartbeat. ``ttl_seconds`` defaults
        to the original TTL — pass an explicit value to grow or shrink
        the window.

        Raises:
            LockNotFound: No row exists for ``name``.
            LockNotHeld: The row exists but is owned by a different
                holder.
        """
        body: dict[str, Any] = {"holder": holder}
        if ttl_seconds is not None:
            body["ttl_seconds"] = ttl_seconds
        response = self._ws._http.post(
            f"/v1/workspaces/{self._ws.id}/locks/{_en(name)}/heartbeat",
            json=body,
            not_found_class=LockNotFound,
        )
        return Lock.model_validate(response.json())

    def release(self, name: str, *, holder: str) -> None:
        """Release a held lock.

        Idempotent: releasing a lock you don't hold (or one that was
        already swept) is a silent no-op.
        """
        self._ws._http.post(
            f"/v1/workspaces/{self._ws.id}/locks/{_en(name)}/release",
            json={"holder": holder},
            not_found_class=WorkspaceNotFound,
        )

    def list(self) -> list[Lock]:
        """Return every lock currently persisted in this workspace."""
        data = self._ws._http.get_json(
            f"/v1/workspaces/{self._ws.id}/locks",
            not_found_class=WorkspaceNotFound,
        )
        return [Lock.model_validate(lock) for lock in data.get("locks", [])]

    def get(self, name: str) -> Lock:
        """Fetch a single lock row.

        Raises:
            LockNotFound: No row exists for ``name``.
        """
        data = self._ws._http.get_json(
            f"/v1/workspaces/{self._ws.id}/locks/{_en(name)}",
            not_found_class=LockNotFound,
        )
        return Lock.model_validate(data)

    # -- context manager ----------------------------------------------

    @contextmanager
    def held(
        self,
        name: str,
        *,
        holder: str,
        ttl_seconds: int = 60,
        wait_ms: int = 0,
        auto_heartbeat: bool = True,
        heartbeat_interval: float = 20.0,
    ) -> Iterator[Lock]:
        """Acquire ``name``, hold it, then release on exit.

        While the body runs, a daemon thread heartbeats every
        ``heartbeat_interval`` seconds (when ``auto_heartbeat`` is
        ``True``). The thread exits cleanly on normal completion *and*
        on exceptions, including :class:`KeyboardInterrupt`.

        Yields:
            The acquired :class:`Lock`.

        Raises:
            LockConflict: If the lock could not be acquired.
        """
        lock = self.acquire(
            name, holder=holder, ttl_seconds=ttl_seconds, wait_ms=wait_ms
        )
        stop_event = threading.Event()
        thread: threading.Thread | None = None

        if auto_heartbeat:

            def _heartbeat_loop() -> None:
                # ``Event.wait`` returns True when ``set()`` is called
                # (clean exit); False on timeout (heartbeat tick).
                while not stop_event.wait(heartbeat_interval):
                    try:
                        self.heartbeat(name, holder=holder)
                    except Exception:
                        # Don't crash the daemon — the next acquire by
                        # another holder will fence us out anyway.
                        return

            thread = threading.Thread(
                target=_heartbeat_loop,
                name=f"plinth-lock-heartbeat:{name}",
                daemon=True,
            )
            thread.start()

        try:
            yield lock
        finally:
            # Stop the heartbeat first so we don't race the release.
            stop_event.set()
            if thread is not None:
                thread.join(timeout=1.0)
            try:
                self.release(name, holder=holder)
            except Exception:
                # Release is idempotent — swallow so the original
                # exception (if any) propagates unmolested.
                pass


# ---------------------------------------------------------------------------
# Workspace facade
# ---------------------------------------------------------------------------


class Workspace:
    """Client-side handle for a Plinth workspace.

    Instances are typically obtained from :meth:`plinth.Plinth.workspace`,
    which performs a get-or-create lookup by name. Direct construction
    is supported for advanced use cases (testing, post-hoc binding to a
    known workspace ID).
    """

    def __init__(
        self,
        model: WorkspaceModel,
        http: HTTPClient,
        *,
        branch_id: str | None = None,
    ) -> None:
        self._model = model
        self._http = http
        self._branch_id = branch_id
        self.kv = KVProxy(self)
        self.files = FilesProxy(self)
        self.locks = LocksProxy(self)
        self._snapshots = SnapshotProxy(self)
        # Lazy proxies — instantiated on first attribute access so the
        # ``channels`` / ``workflows`` modules don't get imported just
        # because someone wrote ``ws.kv.set(...)``.
        self._channels: ChannelsProxy | None = None
        self._workflows: WorkflowsProxy | None = None

    # -- attributes pulled from the model ------------------------------

    @property
    def id(self) -> str:
        """The workspace ID (e.g. ``ws_01H...``)."""
        return self._model.id

    @property
    def name(self) -> str:
        """The human-readable workspace name."""
        return self._model.name

    @property
    def model(self) -> WorkspaceModel:
        """The underlying :class:`Workspace` Pydantic model."""
        return self._model

    @property
    def branch_id(self) -> str | None:
        """If this view is scoped to a branch, the branch ID."""
        return self._branch_id

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        scope = f", branch={self._branch_id!r}" if self._branch_id else ""
        return f"Workspace(id={self.id!r}, name={self.name!r}{scope})"

    # -- internal helpers ----------------------------------------------

    def _branch_params(self) -> dict[str, Any]:
        """Return ``{"branch": <id>}`` if scoped, otherwise empty."""
        return {"branch": self._branch_id} if self._branch_id else {}

    # -- snapshot helpers (top-level for ergonomics) --------------------

    def snapshot(self, name: str, *, message: str | None = None) -> Snapshot:
        """Create a snapshot — sugar for ``ws._snapshots.create``."""
        return self._snapshots.create(name, message=message)

    def snapshots(self) -> list[Snapshot]:
        """List all snapshots in the workspace."""
        return self._snapshots.list()

    def diff(self, snapshot_a: str, snapshot_b: str) -> DiffResult:
        """Diff two snapshots."""
        return self._snapshots.diff(snapshot_a, snapshot_b)

    # -- branch helpers ------------------------------------------------

    def branch(self, name: str, *, from_snapshot: str) -> Branch:
        """Create a branch starting from ``from_snapshot``."""
        response = self._http.post(
            f"/v1/workspaces/{self.id}/branches",
            json={"name": name, "from_snapshot": from_snapshot},
            not_found_class=SnapshotNotFound,
        )
        return Branch.model_validate(response.json())

    def branches(self) -> list[Branch]:
        """List every branch on the workspace."""
        data = self._http.get_json(
            f"/v1/workspaces/{self.id}/branches",
            not_found_class=WorkspaceNotFound,
        )
        return [Branch.model_validate(b) for b in data.get("branches", [])]

    def merge(self, branch_id: str) -> MergeResult:
        """Merge ``branch_id`` back into the workspace's main timeline."""
        response = self._http.post(
            f"/v1/workspaces/{self.id}/branches/{branch_id}/merge",
            not_found_class=BranchNotFound,
        )
        return MergeResult.model_validate(response.json())

    def delete_branch(self, branch_id: str) -> None:
        """Delete a branch without merging."""
        self._http.delete(
            f"/v1/workspaces/{self.id}/branches/{branch_id}",
            not_found_class=BranchNotFound,
        )

    def with_branch(self, branch_id: str) -> Workspace:
        """Return a view of this workspace scoped to ``branch_id``.

        The returned object shares the underlying HTTP client but every
        KV / file / snapshot call automatically appends ``?branch=<id>``.
        """
        return Workspace(self._model, self._http, branch_id=branch_id)

    # -- v0.2: channels / workflows proxies (lazy) ---------------------

    @property
    def channels(self) -> ChannelsProxy:
        """Workspace channels — typed, persistent message queues.

        See :class:`plinth.channels.ChannelsProxy` for the full API.
        """
        if self._channels is None:
            from .channels import ChannelsProxy

            self._channels = ChannelsProxy(self)
        return self._channels

    @property
    def workflows(self) -> WorkflowsProxy:
        """Workspace workflows — durable, resumable agent pipelines.

        See :class:`plinth.workflows.WorkflowsProxy` for the full API.
        """
        if self._workflows is None:
            from .workflows import WorkflowsProxy

            self._workflows = WorkflowsProxy(self)
        return self._workflows


__all__ = [
    "FilesProxy",
    "KVProxy",
    "LocksProxy",
    "SnapshotProxy",
    "Workspace",
]
