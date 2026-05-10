# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Workspace workflows: durable, resumable agent pipelines.

A workflow is a manifest of expected step names plus a server-tracked
log of completed steps. Each step has a lifecycle of
``pending -> running -> (completed | failed | cancelled)`` and may
reference a workspace snapshot at completion time so a crashed agent
can resume from a known checkpoint.

Two public surface elements:

* :class:`WorkflowsProxy` -- reachable via ``ws.workflows``. Owns
  ``create`` / ``get_or_create`` / ``get`` / ``list`` and returns
  :class:`WorkflowHandle` objects (never the bare model) so callers can
  chain step transitions ergonomically.
* :class:`WorkflowHandle` -- a thin wrapper around a :class:`Workflow`
  that exposes method-style ``start_step`` / ``complete_step`` / ...
  Holds a reference to the parent :class:`Workspace` so callers don't
  have to thread it through.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .exceptions import (
    InvalidWorkflowStep,
    WorkflowNotFound,
    WorkflowStepNotFound,
    WorkspaceNotFound,
)
from .models import DLQEntry, Lease, ResumeInfo, Workflow, WorkflowStep

if TYPE_CHECKING:
    from .workspace import Workspace


# ---------------------------------------------------------------------------
# WorkflowHandle
# ---------------------------------------------------------------------------


class WorkflowHandle:
    """Method-style wrapper around a :class:`Workflow`.

    Returned by :meth:`WorkflowsProxy.create`, :meth:`WorkflowsProxy.get`,
    and :meth:`WorkflowsProxy.get_or_create`. Holds a reference to the
    parent :class:`Workspace` so it can issue API calls without forcing
    the caller to pass the workspace through every step transition.

    Mutations (start_step, complete_step, ...) update the cached
    :attr:`model` so subsequent reads against the handle are consistent.
    """

    def __init__(self, workspace: Workspace, model: Workflow) -> None:
        self._ws = workspace
        self._wf = model
        # v1.1 — per-step retry config supplied at workflow create time.
        # Populated by :meth:`WorkflowsProxy.create` when the caller
        # passes a list of dicts; left empty for the v1.0 list[str]
        # path. Looked up by step name in :meth:`start_step` so the
        # caller doesn't have to repeat the config on every start.
        self._retry_config: dict[str, dict[str, Any]] = {}

    # -- attributes ----------------------------------------------------

    @property
    def id(self) -> str:
        """The workflow ID (``wf_<ulid>``)."""
        return self._wf.id

    @property
    def name(self) -> str:
        """The workflow name."""
        return self._wf.name

    @property
    def status(self) -> str:
        """Current workflow status from the cached model.

        This reflects the server's view as of the last refresh -- call
        :meth:`refresh` if you need to re-read state after an external
        change.
        """
        return self._wf.status

    @property
    def steps(self) -> list[WorkflowStep]:
        """The cached list of recorded :class:`WorkflowStep` entries."""
        return self._wf.steps

    @property
    def steps_manifest(self) -> list[str]:
        """The expected step names in declaration order."""
        return self._wf.steps_manifest

    @property
    def metadata(self) -> dict[str, Any]:
        """The workflow's free-form metadata dict."""
        return self._wf.metadata

    @property
    def model(self) -> Workflow:
        """The underlying :class:`Workflow` Pydantic model (cached)."""
        return self._wf

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"WorkflowHandle(id={self.id!r}, name={self.name!r}, "
            f"status={self.status!r})"
        )

    # -- step transitions ----------------------------------------------

    def start_step(
        self,
        name: str,
        input: Any = None,  # noqa: A002 - mirrors API field name
        *,
        snapshot_id: str | None = None,
        initial_status: str = "running",
        max_attempts: int = 1,
        retry_policy: str = "none",
        retry_initial_delay_seconds: float = 1.0,
        retry_max_delay_seconds: float = 60.0,
        retry_jitter: bool = True,
    ) -> WorkflowStep:
        """Create a step on this workflow.

        ``initial_status`` defaults to ``"running"`` (the v0.2 in-process
        flow where the agent starts work immediately). Pass
        ``initial_status="pending"`` for the durable workflow executor:
        the step will be visible to workers via ``pending_steps()`` and
        will only flip to ``running`` when a worker leases it.

        A subsequent :meth:`complete_step` / :meth:`fail_step` /
        :meth:`cancel_step` advances the step to a terminal state.

        v1.1: ``max_attempts`` / ``retry_policy`` /
        ``retry_initial_delay_seconds`` / ``retry_max_delay_seconds`` /
        ``retry_jitter`` configure the per-step retry policy. Defaults
        preserve v1.0 behaviour (``max_attempts=1``).

        Args:
            name: Must be one of the names in :attr:`steps_manifest`.
            input: Optional input payload to record on the step.
            snapshot_id: Optional snapshot taken before the step ran.
            initial_status: ``"running"`` (default) | ``"pending"``.
            max_attempts: Total attempts before the step lands in the DLQ.
            retry_policy: ``"none"`` | ``"exponential"`` | ``"fixed"``.
            retry_initial_delay_seconds: First retry delay base.
            retry_max_delay_seconds: Cap applied after exponential growth.
            retry_jitter: When True, ±25% jitter is applied to each delay.

        Raises:
            InvalidWorkflowStep: When ``name`` is not part of the manifest.
        """
        if self._wf.steps_manifest and name not in self._wf.steps_manifest:
            raise InvalidWorkflowStep(
                f"Step {name!r} is not declared in the workflow manifest "
                f"{list(self._wf.steps_manifest)!r}.",
            )

        # If the workflow was created with per-step retry config, fall
        # back to that config when the caller didn't override it. The
        # explicit kwargs always win over the cached config.
        cached = self._retry_config.get(name, {})
        if max_attempts == 1 and "max_attempts" in cached:
            max_attempts = int(cached["max_attempts"])
        if retry_policy == "none" and "retry_policy" in cached:
            retry_policy = str(cached["retry_policy"])
        if (
            retry_initial_delay_seconds == 1.0
            and "retry_initial_delay_seconds" in cached
        ):
            retry_initial_delay_seconds = float(
                cached["retry_initial_delay_seconds"]
            )
        if (
            retry_max_delay_seconds == 60.0
            and "retry_max_delay_seconds" in cached
        ):
            retry_max_delay_seconds = float(cached["retry_max_delay_seconds"])
        if retry_jitter is True and "retry_jitter" in cached:
            retry_jitter = bool(cached["retry_jitter"])

        body: dict[str, Any] = {"name": name, "initial_status": initial_status}
        if input is not None:
            body["input"] = input
        if snapshot_id is not None:
            body["snapshot_id"] = snapshot_id
        # v1.1 retry params — only sent when not the v1.0 defaults so the
        # request body stays small for callers that haven't opted in.
        if max_attempts != 1:
            body["max_attempts"] = max_attempts
        if retry_policy != "none":
            body["retry_policy"] = retry_policy
        if retry_initial_delay_seconds != 1.0:
            body["retry_initial_delay_seconds"] = retry_initial_delay_seconds
        if retry_max_delay_seconds != 60.0:
            body["retry_max_delay_seconds"] = retry_max_delay_seconds
        if retry_jitter is not True:
            body["retry_jitter"] = retry_jitter

        response = self._ws._http.post(
            f"/v1/workspaces/{self._ws.id}/workflows/{self.id}/steps",
            json=body,
            not_found_class=WorkflowNotFound,
        )
        step = WorkflowStep.model_validate(response.json())
        self._record_step(step)
        return step

    def complete_step(
        self,
        step_id: str,
        output: Any = None,
        *,
        snapshot_id: str | None = None,
    ) -> WorkflowStep:
        """Mark ``step_id`` completed, recording its output and snapshot.

        ``snapshot_id`` is the canonical resume point -- :meth:`resume_info`
        will surface it to the next agent.
        """
        return self._patch_step(
            step_id,
            status="completed",
            output=output,
            snapshot_id=snapshot_id,
        )

    def fail_step(
        self,
        step_id: str,
        error: str,
        *,
        output: Any = None,
    ) -> WorkflowStep:
        """Mark ``step_id`` failed with ``error``."""
        return self._patch_step(
            step_id,
            status="failed",
            error=error,
            output=output,
        )

    def cancel_step(self, step_id: str) -> WorkflowStep:
        """Mark ``step_id`` cancelled."""
        return self._patch_step(step_id, status="cancelled")

    def _patch_step(
        self,
        step_id: str,
        *,
        status: str,
        output: Any = None,
        error: str | None = None,
        snapshot_id: str | None = None,
    ) -> WorkflowStep:
        """Internal: PATCH a step into a new state."""
        body: dict[str, Any] = {"status": status}
        if output is not None:
            body["output"] = output
        if error is not None:
            body["error"] = error
        if snapshot_id is not None:
            body["snapshot_id"] = snapshot_id

        # The HTTP wrapper does not expose a verb-specific PATCH helper,
        # so fall through to the underlying httpx client and reuse its
        # error-mapping path.
        response = self._ws._http._client.request(
            "PATCH",
            f"/v1/workspaces/{self._ws.id}/workflows/{self.id}/steps/{step_id}",
            json=body,
        )
        self._ws._http._raise_for_status(
            response,
            not_found_class=WorkflowStepNotFound,
        )
        step = WorkflowStep.model_validate(response.json())
        self._record_step(step)
        return step

    # -- whole-workflow operations -------------------------------------

    def cancel(self) -> None:
        """Cancel the entire workflow.

        Refreshes the cached :class:`Workflow` so :attr:`status` reflects
        the new server state.
        """
        response = self._ws._http.post(
            f"/v1/workspaces/{self._ws.id}/workflows/{self.id}/cancel",
            not_found_class=WorkflowNotFound,
        )
        self._wf = Workflow.model_validate(response.json())

    def resume_info(self) -> ResumeInfo:
        """Return the next step + snapshot to resume from after a crash."""
        data = self._ws._http.get_json(
            f"/v1/workspaces/{self._ws.id}/workflows/{self.id}/resume",
            not_found_class=WorkflowNotFound,
        )
        return ResumeInfo.model_validate(data)

    def refresh(self) -> None:
        """Re-fetch the underlying :class:`Workflow` from the server."""
        data = self._ws._http.get_json(
            f"/v1/workspaces/{self._ws.id}/workflows/{self.id}",
            not_found_class=WorkflowNotFound,
        )
        self._wf = Workflow.model_validate(data)

    # -- v0.5: lease + worker integration ------------------------------

    def pending_steps(self) -> list[WorkflowStep]:
        """List steps in ``pending`` status (ready for a worker to lease).

        Workers use this to discover work; the v0.2 in-process flow
        creates steps in ``running`` directly so the list is empty
        unless a worker is in the loop.
        """

        data = self._ws._http.get_json(
            f"/v1/workspaces/{self._ws.id}/workflows/{self.id}/pending",
            not_found_class=WorkflowNotFound,
        )
        return [WorkflowStep.model_validate(s) for s in data.get("steps", [])]

    def expired_leases(self) -> list[Lease]:
        """Return leases past their expiry that haven't been reaped yet."""
        data = self._ws._http.get_json(
            f"/v1/workspaces/{self._ws.id}/workflows/{self.id}/expired",
            not_found_class=WorkflowNotFound,
        )
        return [Lease.model_validate(le) for le in data.get("leases", [])]

    def lease_step(
        self,
        step_id: str,
        worker_id: str,
        *,
        ttl: int = 60,
    ) -> Lease | None:
        """Try to acquire a lease on ``step_id`` for ``worker_id``.

        Returns the new :class:`Lease` on success or ``None`` on a 409
        ``LEASE_CONFLICT`` (someone else got it). All other errors
        propagate as the corresponding :class:`PlinthError` subclass.
        """

        from .exceptions import LeaseConflict

        try:
            response = self._ws._http.post(
                f"/v1/workspaces/{self._ws.id}/workflows/{self.id}"
                f"/steps/{step_id}/lease",
                json={"worker_id": worker_id, "ttl_seconds": ttl},
                not_found_class=WorkflowStepNotFound,
            )
        except LeaseConflict:
            return None
        return Lease.model_validate(response.json())

    def heartbeat_step(
        self,
        step_id: str,
        worker_id: str,
        *,
        ttl: int | None = None,
    ) -> Lease:
        """Extend the lease on ``step_id`` (must be held by ``worker_id``)."""
        body: dict[str, Any] = {"worker_id": worker_id}
        if ttl is not None:
            body["ttl_seconds"] = ttl
        response = self._ws._http.post(
            f"/v1/workspaces/{self._ws.id}/workflows/{self.id}"
            f"/steps/{step_id}/heartbeat",
            json=body,
            not_found_class=WorkflowStepNotFound,
        )
        return Lease.model_validate(response.json())

    def release_step(
        self,
        step_id: str,
        worker_id: str,
        *,
        status: str = "completed",
        output: Any = None,
        error: str | None = None,
        snapshot_id: str | None = None,
    ) -> Lease:
        """Release the lease, marking the step ``status`` (or pending to retry)."""
        body: dict[str, Any] = {"worker_id": worker_id, "status": status}
        if output is not None:
            body["output"] = output
        if error is not None:
            body["error"] = error
        if snapshot_id is not None:
            body["snapshot_id"] = snapshot_id
        response = self._ws._http.post(
            f"/v1/workspaces/{self._ws.id}/workflows/{self.id}"
            f"/steps/{step_id}/release",
            json=body,
            not_found_class=WorkflowStepNotFound,
        )
        lease = Lease.model_validate(response.json())
        # Refresh cached steps so callers can read the new status.
        self.refresh()
        return lease

    # -- v1.1: dead-letter queue ---------------------------------------

    def dlq(self) -> list[DLQEntry]:
        """List every DLQ entry recorded for this workflow.

        Entries are returned newest-first by ``failed_at``. An empty
        list is returned when no step has yet exhausted its retries.
        """
        data = self._ws._http.get_json(
            f"/v1/workspaces/{self._ws.id}/workflows/{self.id}/dlq",
            not_found_class=WorkflowNotFound,
        )
        return [DLQEntry.model_validate(e) for e in data.get("entries", [])]

    def replay_dlq(self, dlq_id: str) -> WorkflowStep:
        """Replay ``dlq_id`` as a fresh attempt of the same step name.

        The server creates a new step row in ``pending`` status (so a
        worker can immediately lease it) and deletes the DLQ entry in
        the same transaction. Returns the new :class:`WorkflowStep`.
        """
        response = self._ws._http.post(
            f"/v1/workspaces/{self._ws.id}/workflows/{self.id}"
            f"/dlq/{dlq_id}/replay",
            not_found_class=WorkflowNotFound,
        )
        body = response.json()
        # Refresh the cached workflow so the new step lands in the
        # handle's step log.
        self.refresh()
        return WorkflowStep.model_validate(body["replayed_step"])

    def delete_dlq(self, dlq_id: str) -> None:
        """Delete a DLQ entry without replaying it (operator dismissal)."""
        # No JSON response is expected (204), so we drop down to the
        # raw httpx client.
        response = self._ws._http._client.request(
            "DELETE",
            f"/v1/workspaces/{self._ws.id}/workflows/{self.id}/dlq/{dlq_id}",
        )
        self._ws._http._raise_for_status(
            response,
            not_found_class=WorkflowNotFound,
        )

    # -- internal ------------------------------------------------------

    def _record_step(self, step: WorkflowStep) -> None:
        """Update the cached steps list with the latest server view."""
        existing = self._wf.steps
        for i, s in enumerate(existing):
            if s.id == step.id:
                existing[i] = step
                return
        existing.append(step)


# ---------------------------------------------------------------------------
# WorkflowsProxy -- exposed via ``ws.workflows``.
# ---------------------------------------------------------------------------


class WorkflowsProxy:
    """API surface for the workspace's workflows.

    Held as ``ws.workflows`` (lazily instantiated on first attribute
    access). Returns :class:`WorkflowHandle` objects -- never the raw
    model -- so callers can chain step transitions in one expression.
    """

    def __init__(self, workspace: Workspace) -> None:
        self._ws = workspace

    # ------------------------------------------------------------------
    # create / get_or_create / get / list
    # ------------------------------------------------------------------

    def create(
        self,
        name: str,
        steps: list[str] | list[dict[str, Any]],
        metadata: dict[str, Any] | None = None,
    ) -> WorkflowHandle:
        """Create a new workflow with the given step manifest.

        v1.1: ``steps`` may now be a list of dicts carrying per-step
        retry configuration. The dict shape is::

            {"name": "search", "max_attempts": 3,
             "retry_policy": "exponential",
             "retry_initial_delay_seconds": 2.0,
             "retry_max_delay_seconds": 60.0,
             "retry_jitter": True}

        Only ``name`` is required. The retry config is cached on the
        handle so subsequent :meth:`WorkflowHandle.start_step` calls
        forward it to the server when the matching step is started.
        Bare-string entries are still accepted (v1.0 behaviour) — those
        steps default to ``max_attempts=1`` (no retry).

        Args:
            name: A human-readable name (e.g. ``"research-pipeline"``).
            steps: Ordered manifest. Either a list of step names, or a
                list of per-step config dicts (see above). Each
                ``start_step`` call must use a name from this list.
            metadata: Optional free-form metadata dict.

        Returns:
            A :class:`WorkflowHandle` wrapping the newly created workflow.
        """
        # Normalise: server expects ``list[str]`` for ``steps``. Per-step
        # retry config is cached on the handle so start_step() can pull
        # it back out without an extra round-trip.
        names: list[str] = []
        retry_cfg: dict[str, dict[str, Any]] = {}
        for entry in steps:
            if isinstance(entry, str):
                names.append(entry)
                continue
            if not isinstance(entry, dict) or "name" not in entry:
                raise ValueError(
                    "steps entries must be a string or a dict with a "
                    "'name' key"
                )
            names.append(entry["name"])
            cfg = {k: v for k, v in entry.items() if k != "name"}
            if cfg:
                retry_cfg[entry["name"]] = cfg

        body: dict[str, Any] = {"name": name, "steps": names}
        if metadata is not None:
            body["metadata"] = metadata

        response = self._ws._http.post(
            f"/v1/workspaces/{self._ws.id}/workflows",
            json=body,
            not_found_class=WorkspaceNotFound,
        )
        handle = WorkflowHandle(self._ws, Workflow.model_validate(response.json()))
        # Stash the retry config on the handle so start_step() can pick
        # it up by step name.
        handle._retry_config = retry_cfg  # type: ignore[attr-defined]
        return handle

    def get_or_create(
        self,
        name: str,
        steps: list[str],
        metadata: dict[str, Any] | None = None,
    ) -> WorkflowHandle:
        """Idempotent create-by-name: if a workflow with ``name`` already
        exists, fetch and return it; otherwise create one.

        The list lookup is best-effort -- if multiple workflows share the
        same name (legacy or test data), the first one returned by the
        server wins. New code should use unique names.
        """
        existing = next(
            (w for w in self.list() if w.name == name),
            None,
        )
        if existing is not None:
            # Re-fetch to load the full step log; ``list`` typically
            # returns a slimmer summary.
            return self.get(existing.id)
        return self.create(name, steps, metadata)

    def get(self, workflow_id: str) -> WorkflowHandle:
        """Fetch a workflow by ID (404 -> :class:`WorkflowNotFound`)."""
        data = self._ws._http.get_json(
            f"/v1/workspaces/{self._ws.id}/workflows/{workflow_id}",
            not_found_class=WorkflowNotFound,
        )
        return WorkflowHandle(self._ws, Workflow.model_validate(data))

    def list(self) -> list[Workflow]:
        """List every workflow on the workspace as :class:`Workflow` rows.

        Returns the bare model so the call cost is one request -- to act
        on a workflow, follow up with :meth:`get` for a
        :class:`WorkflowHandle`.
        """
        data = self._ws._http.get_json(
            f"/v1/workspaces/{self._ws.id}/workflows",
            not_found_class=WorkspaceNotFound,
        )
        return [Workflow.model_validate(w) for w in data.get("workflows", [])]


__all__ = ["WorkflowHandle", "WorkflowsProxy"]
