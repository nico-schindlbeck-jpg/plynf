# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Workflow handler registry + dispatch for the durable workflow executor.

A :class:`WorkflowRuntime` keeps a mapping of ``(workflow_name, step_name)``
to a Python callable. The :func:`Plinth.workflow_handler` decorator on the
top-level facade registers handlers into the client's runtime; the worker
process imports the user's handlers module (which triggers the
decorations), then iterates pending steps via the workspace API and calls
:meth:`WorkflowRuntime.dispatch` for each leased step.

The handler signature is::

    def handler(ctx: HandlerContext) -> Any: ...

``ctx`` exposes a scoped Plinth client + the step row + the workspace
+ workflow handles, so the handler doesn't need to thread these through
manually. Handlers may be sync OR async; the dispatcher awaits async
results transparently.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .exceptions import NoHandlerError
from .models import WorkflowStep

if TYPE_CHECKING:
    from .client import Plinth
    from .workflows import WorkflowHandle
    from .workspace import Workspace


HandlerFn = Callable[["HandlerContext"], Any]


# ---------------------------------------------------------------------------
# Handler context
# ---------------------------------------------------------------------------


class HandlerContext:
    """Per-step execution context passed into a workflow handler.

    The handler receives one argument — this object. From it, the handler
    can:

    * read step input (``ctx.step.input``)
    * mutate the workspace (``ctx.workspace.kv.set(...)``,
      ``ctx.workspace.files.write(...)``, ``ctx.workspace.snapshot(...)``)
    * call tools (``ctx.tools.invoke(...)``)
    * inspect the workflow (``ctx.workflow.id``, ``ctx.workflow.steps_manifest``)
    * identify itself (``ctx.worker_id``)

    The return value of the handler becomes the step's ``output``. Raising
    any exception from the handler marks the step ``failed`` with the
    exception's ``str(exc)`` as the error message.
    """

    def __init__(
        self,
        *,
        client: Plinth,
        workspace: Workspace,
        workflow: WorkflowHandle,
        step: WorkflowStep,
        worker_id: str,
    ) -> None:
        self.client = client
        self.workspace = workspace
        self.workflow = workflow
        self.step = step
        self.worker_id = worker_id

    @property
    def tools(self) -> Any:
        """Shorthand for ``ctx.client.tools``."""
        return self.client.tools

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"HandlerContext(workflow={self.workflow.name!r}, "
            f"step={self.step.name!r}, worker={self.worker_id!r})"
        )


# ---------------------------------------------------------------------------
# Runtime registry
# ---------------------------------------------------------------------------


class WorkflowRuntime:
    """Registry of workflow step handlers.

    Each :class:`Plinth` client owns one :class:`WorkflowRuntime`. The
    runtime is also accessible as ``plinth.workflow_runtime`` so worker
    code (which only imports the handlers module — not the client) can
    reach the table by going through the same ``client`` instance the
    handlers were decorated with.

    Handlers are keyed by ``(workflow_name, step_name)``. Re-registering
    the same key raises :class:`ValueError` to surface deployment-time
    typos rather than silently shadow earlier registrations.
    """

    def __init__(self) -> None:
        self._handlers: dict[tuple[str, str], HandlerFn] = {}

    def register(
        self,
        workflow: str,
        step: str,
    ) -> Callable[[HandlerFn], HandlerFn]:
        """Return a decorator that registers ``fn`` as the handler for
        ``(workflow, step)``.

        Idempotent within a single registration: calling the same
        decorator twice on different functions raises :class:`ValueError`.
        """

        if not workflow or not step:
            raise ValueError("workflow and step must be non-empty strings")

        def decorator(fn: HandlerFn) -> HandlerFn:
            key = (workflow, step)
            if key in self._handlers:
                raise ValueError(
                    f"handler already registered for {key!r} "
                    f"(existing={self._handlers[key].__qualname__}, "
                    f"new={fn.__qualname__})"
                )
            self._handlers[key] = fn
            return fn

        return decorator

    def get(self, workflow: str, step: str) -> HandlerFn | None:
        """Look up a handler without raising. Returns ``None`` if none."""
        return self._handlers.get((workflow, step))

    def has(self, workflow: str, step: str) -> bool:
        """Return ``True`` iff a handler for ``(workflow, step)`` exists."""
        return (workflow, step) in self._handlers

    def keys(self) -> list[tuple[str, str]]:
        """Return all registered ``(workflow, step)`` keys."""
        return list(self._handlers.keys())

    def __len__(self) -> int:
        return len(self._handlers)

    def __contains__(self, key: tuple[str, str]) -> bool:
        return key in self._handlers

    async def dispatch(
        self,
        workflow: str,
        step: str,
        ctx: HandlerContext,
    ) -> Any:
        """Run the registered handler for ``(workflow, step)``.

        Awaits the result if the handler is a coroutine function. Raises
        :class:`NoHandlerError` if no handler is registered.
        """

        fn = self._handlers.get((workflow, step))
        if fn is None:
            raise NoHandlerError(
                f"no handler registered for ({workflow!r}, {step!r})",
                details={
                    "workflow": workflow,
                    "step": step,
                    "available": [list(k) for k in self._handlers],
                },
            )
        result = fn(ctx)
        if inspect.isawaitable(result):
            return await result
        return result


__all__ = [
    "HandlerContext",
    "HandlerFn",
    "WorkflowRuntime",
]
