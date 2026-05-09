# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""The top-level :class:`Plinth` facade.

The facade owns one :class:`HTTPClient` per backing service and exposes
the four primary entry points users need: workspace get-or-create, the
tool gateway, token counting, and the ``@agent`` decorator.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

import httpx

from . import tokens as tokens_module
from ._http import HTTPClient
from .agent import agent_decorator
from .identity import IdentityClient
from .models import Workspace as WorkspaceModel
from .tools import ToolGateway
from .workers import WorkersClient
from .workflow_runtime import WorkflowRuntime
from .workspace import Workspace

F = TypeVar("F", bound=Callable[..., Any])


DEFAULT_WORKSPACE_URL = "http://localhost:7421"
DEFAULT_GATEWAY_URL = "http://localhost:7422"
DEFAULT_IDENTITY_URL = "http://localhost:7425"
DEFAULT_TIMEOUT = 30.0


class Plinth:
    """Single entry point for the Plinth runtime.

    Args:
        workspace_url: Base URL of the workspace service.
        gateway_url: Base URL of the gateway service.
        api_key: Bearer token for both services. In the v0.1 PoC any
            non-empty string is accepted; a real deployment will issue
            scoped tokens.
        timeout: Per-request timeout in seconds for both services.
        workspace_transport: Optional ``httpx`` transport for the
            workspace client (used by tests with ``respx``).
        gateway_transport: Optional ``httpx`` transport for the gateway
            client.

    Example::

        client = Plinth(api_key="local-dev")
        ws = client.workspace("research-task-1")
        ws.kv.set("topic", "renewable energy")
    """

    def __init__(
        self,
        *,
        workspace_url: str = DEFAULT_WORKSPACE_URL,
        gateway_url: str = DEFAULT_GATEWAY_URL,
        identity_url: str | None = None,
        api_key: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        workspace_transport: httpx.BaseTransport | None = None,
        gateway_transport: httpx.BaseTransport | None = None,
        identity_transport: httpx.BaseTransport | None = None,
        # v1.0 — multi-region. ``region`` is the primary region this
        # client is "homed" against. ``fallback_regions`` is an ordered
        # list of region ids to try on connection failures or replica
        # redirects (the SDK reads ``X-Plinth-Primary-Region`` and routes
        # accordingly). ``fallback_*_urls`` are dicts mapping each
        # fallback region id to its workspace/gateway URL — keeping them
        # split per service mirrors the production deploy where the
        # workspace and gateway live behind separate DNS names.
        region: str | None = None,
        fallback_regions: list[str] | None = None,
        fallback_workspace_urls: dict[str, str] | None = None,
        fallback_gateway_urls: dict[str, str] | None = None,
        fallback_identity_urls: dict[str, str] | None = None,
    ) -> None:
        if not api_key:
            # Surface the issue at construction rather than on first
            # request: a missing API key is a configuration mistake.
            raise ValueError(
                "api_key is required. In local dev pass any non-empty string, "
                "e.g. Plinth(api_key='local-dev')."
            )

        self._workspace_url = workspace_url
        self._gateway_url = gateway_url
        self._identity_url = identity_url
        self._api_key = api_key
        self._timeout = timeout
        self._region = region
        self._fallback_regions = list(fallback_regions or [])

        # Build the per-service fallback maps in the order spec'd by the
        # operator. Unknown regions are silently dropped so a typo'd
        # ``fallback_regions=["us"]`` without a matching URL doesn't trip
        # a spurious connection attempt.
        ws_fallbacks = _ordered_fallback_urls(
            self._fallback_regions, fallback_workspace_urls
        )
        gw_fallbacks = _ordered_fallback_urls(
            self._fallback_regions, fallback_gateway_urls
        )
        identity_fallbacks = _ordered_fallback_urls(
            self._fallback_regions, fallback_identity_urls
        )

        self._workspace_http = HTTPClient(
            workspace_url,
            api_key,
            timeout=timeout,
            transport=workspace_transport,
            fallback_urls=ws_fallbacks,
            primary_region=region,
        )
        self._gateway_http = HTTPClient(
            gateway_url,
            api_key,
            timeout=timeout,
            transport=gateway_transport,
            fallback_urls=gw_fallbacks,
            primary_region=region,
        )
        # Stash for the identity client below.
        self._identity_fallbacks = identity_fallbacks

        self.tools = ToolGateway(self._gateway_http)
        # ``client.gateway`` is the v0.5 alias for ``client.tools``: it
        # carries the workflow transaction factory + transactions client
        # alongside the existing tool surface, matching the contract in
        # CONTRACTS.md ("client.gateway.transaction(...)").
        self.gateway = self.tools

        # v0.5 — durable workflow executor. ``workers`` talks to the
        # workspace's ``/v1/workers`` endpoints. ``_workflow_runtime`` is
        # the registry the ``@workflow_handler`` decorator populates;
        # the worker process imports the user's handlers module to
        # populate it then loops on the workspace.
        self.workers = WorkersClient(self._workspace_http)
        self._workflow_runtime = WorkflowRuntime()

        # Identity is opt-in. Most app-level code only needs the long-lived
        # ``api_key``; the identity client is for ops/test code that mints
        # short-lived capability tokens.
        self.identity: IdentityClient | None = None
        if identity_url:
            self.identity = IdentityClient(
                identity_url,
                api_key=api_key,
                timeout=timeout,
                transport=identity_transport,
                fallback_urls=self._identity_fallbacks,
                primary_region=region,
            )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close all underlying HTTP clients."""
        self._workspace_http.close()
        self._gateway_http.close()
        if self.identity is not None:
            self.identity.close()

    def __enter__(self) -> Plinth:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Workspaces
    # ------------------------------------------------------------------

    def workspace(self, name: str) -> Workspace:
        """Get a workspace by name, creating it if it does not exist.

        PoC behaviour: queries every workspace and filters by name. If
        multiple workspaces share the same name, the most recently
        updated one wins. Production deployments will scope the lookup
        per agent.

        Args:
            name: The human-readable workspace name.

        Returns:
            A :class:`Workspace` ready to use.
        """
        existing = self._find_workspace_by_name(name)
        if existing is not None:
            return Workspace(existing, self._workspace_http)
        return Workspace(self._create_workspace(name), self._workspace_http)

    def get_workspace(self, workspace_id: str) -> Workspace:
        """Fetch an existing workspace by ID."""
        from .exceptions import WorkspaceNotFound

        data = self._workspace_http.get_json(
            f"/v1/workspaces/{workspace_id}",
            not_found_class=WorkspaceNotFound,
        )
        return Workspace(WorkspaceModel.model_validate(data), self._workspace_http)

    def list_workspaces(self) -> list[Workspace]:
        """List all workspaces visible to this client."""
        data = self._workspace_http.get_json("/v1/workspaces")
        return [
            Workspace(WorkspaceModel.model_validate(w), self._workspace_http)
            for w in data.get("workspaces", [])
        ]

    def delete_workspace(self, workspace_id: str) -> None:
        """Delete a workspace by ID."""
        from .exceptions import WorkspaceNotFound

        self._workspace_http.delete(
            f"/v1/workspaces/{workspace_id}",
            not_found_class=WorkspaceNotFound,
        )

    # ------------------------------------------------------------------
    # Tenants
    # ------------------------------------------------------------------

    def tenants_list(self) -> list[dict[str, Any]]:
        """List tenants visible to this client (via the workspace service).

        Returns dicts of the shape ``{"id": str, "workspace_count": int}``
        as exposed by the workspace's ``GET /v1/tenants`` endpoint. Use the
        identity client (``client.identity.list_tenants()``) for the full
        tenant metadata.
        """
        data = self._workspace_http.get_json("/v1/tenants")
        return list(data.get("tenants", []))

    # -- internal helpers ---------------------------------------------

    def _find_workspace_by_name(self, name: str) -> WorkspaceModel | None:
        """Return the most recently updated workspace with ``name``."""
        data = self._workspace_http.get_json("/v1/workspaces")
        candidates = [
            WorkspaceModel.model_validate(w)
            for w in data.get("workspaces", [])
            if w.get("name") == name
        ]
        if not candidates:
            return None
        # If multiple match (legacy data, race conditions), prefer the
        # latest one — the deterministic tiebreak keeps tests stable.
        candidates.sort(key=lambda w: w.updated_at, reverse=True)
        return candidates[0]

    def _create_workspace(self, name: str) -> WorkspaceModel:
        """POST a new workspace and return the materialised model."""
        response = self._workspace_http.post(
            "/v1/workspaces",
            json={"name": name},
        )
        return WorkspaceModel.model_validate(response.json())

    # ------------------------------------------------------------------
    # Token counting & cost estimation
    # ------------------------------------------------------------------

    def count_tokens(self, text: str) -> int:
        """Return the token count of ``text`` (cl100k_base, offline)."""
        return tokens_module.count(text)

    def estimate_cost(
        self,
        prompt_tokens: int,
        completion_tokens: int = 0,
    ) -> float:
        """Estimate USD cost for a Sonnet request.

        Wraps :func:`plinth.tokens.estimate_cost` so callers don't have
        to import the submodule.
        """
        return tokens_module.estimate_cost(prompt_tokens, completion_tokens)

    # ------------------------------------------------------------------
    # Agent decorator
    # ------------------------------------------------------------------

    def agent(self, *, workspace: str, agent_id: str | None = None) -> Callable[[F], F]:
        """Return a decorator that wires the function into Plinth.

        See :mod:`plinth.agent` for full documentation.
        """
        return agent_decorator(self, workspace=workspace, agent_id=agent_id)

    # ------------------------------------------------------------------
    # v0.5 — Durable workflow executor: handler registration
    # ------------------------------------------------------------------

    def workflow_handler(self, workflow: str, step: str) -> Callable[[F], F]:
        """Register ``fn`` as the handler for a workflow's named step.

        Decorated functions take a single :class:`~plinth.workflow_runtime.HandlerContext`
        argument and return the step's output. The worker process
        imports the user's handlers module on startup, which triggers
        these registrations into ``client._workflow_runtime``; the
        worker then polls pending steps + dispatches via the runtime.

        Example::

            client = Plinth(api_key="local-dev")

            @client.workflow_handler("research-pipeline", step="search")
            def search_step(ctx):
                topic = ctx.step.input["topic"]
                return ctx.tools.invoke("web.search", {"query": topic, "k": 5})

        Re-registering the same ``(workflow, step)`` key raises
        :class:`ValueError` so deployment-time typos surface immediately.
        """

        return self._workflow_runtime.register(workflow, step)  # type: ignore[return-value]

    @property
    def workflow_runtime(self) -> WorkflowRuntime:
        """The :class:`WorkflowRuntime` registry for this client.

        Worker harnesses read this to dispatch leased steps. Application
        code rarely touches it directly — register handlers via
        :meth:`workflow_handler`.
        """

        return self._workflow_runtime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ordered_fallback_urls(
    fallback_regions: list[str],
    fallback_url_map: dict[str, str] | None,
) -> dict[str, str]:
    """Return a dict of ``{region_id: url}`` in ``fallback_regions`` order.

    Regions in ``fallback_regions`` that have no entry in
    ``fallback_url_map`` are silently dropped — operators typically
    configure both maps with the same regions, but a partial config
    shouldn't crash the SDK.
    """

    if not fallback_url_map:
        return {}
    out: dict[str, str] = {}
    for region in fallback_regions:
        url = fallback_url_map.get(region)
        if url:
            out[region] = url
    return out


__all__ = [
    "DEFAULT_GATEWAY_URL",
    "DEFAULT_IDENTITY_URL",
    "DEFAULT_TIMEOUT",
    "DEFAULT_WORKSPACE_URL",
    "Plinth",
]
