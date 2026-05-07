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
from .models import Lease, ResumeInfo, Workflow, WorkflowStep

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
    ) -> WorkflowStep:
        """Create a step on this workflow.

        ``initial_status`` defaults to ``"running"`` (the v0.2 in-process
        flow where the agent starts work immediately). Pass
        ``initial_status="pending"`` for the durable workflow executor:
        the step will be visible to workers via ``pending_steps()`` and
        will only flip to ``running`` when a worker leases it.

        A subsequent :meth:`complete_step` / :meth:`fail_step` /
        :meth:`cancel_step` advances the step to a terminal state.

        Args:
            name: Must be one of the names in :attr:`steps_manifest`.
            input: Optional input payload to record on the step.
            snapshot_id: Optional snapshot taken before the step ran.
            initial_status: ``"running"`` (default) | ``"pending"``.

        Raises:
            InvalidWorkflowStep: When ``name`` is not part of the manifest.
        """
        if self._wf.steps_manifest and name not in self._wf.steps_manifest:
            raise InvalidWorkflowStep(
                f"Step {name!r} is not declared in the workflow manifest "
                f"{list(self._wf.steps_manifest)!r}.",
            )

        body: dict[str, Any] = {"name": name, "initial_status": initial_status}
        if input is not None:
            body["input"] = input
        if snapshot_id is not None:
            body["snapshot_id"] = snapshot_id

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
        steps: list[str],
        metadata: dict[str, Any] | None = None,
    ) -> WorkflowHandle:
        """Create a new workflow with the given step manifest.

        Args:
            name: A human-readable name (e.g. ``"research-pipeline"``).
            steps: Ordered list of step names -- the manifest. Each
                ``start_step`` call must use a name from this list.
            metadata: Optional free-form metadata dict.

        Returns:
            A :class:`WorkflowHandle` wrapping the newly created workflow.
        """
        body: dict[str, Any] = {"name": name, "steps": list(steps)}
        if metadata is not None:
            body["metadata"] = metadata

        response = self._ws._http.post(
            f"/v1/workspaces/{self._ws.id}/workflows",
            json=body,
            not_found_class=WorkspaceNotFound,
        )
        return WorkflowHandle(self._ws, Workflow.model_validate(response.json()))

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
