# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Workflow Transactions client for the Plinth Tool Gateway.

A *transaction* groups multiple tool invocations as a single unit of
work. Each call may register a *compensation* — a tool call to invoke if
the transaction fails partway through. On commit, the gateway runs every
forward call in seq order; on partial failure, executed calls' compensations
fire in reverse (the Saga pattern).

The public surface is :class:`TransactionBuilder`, instantiated via
``client.gateway.transaction(...)``. Pre-existing transactions are managed
through :class:`TransactionsClient`, exposed as
``client.gateway.transactions``.

Example::

    tx = client.gateway.transaction(workspace_id=ws.id, agent_id="my-agent")
    tx.add(
        "github.create_issue",
        {"repo": "owner/name", "title": "..."},
        compensation=("github.update_issue", {
            "repo": "owner/name",
            "issue_number": "{result.number}",
            "state": "closed",
        }),
    )
    tx.add(
        "slack.post_message",
        {"channel": "C123", "text": "Issue created: {seq.0.result.html_url}"},
    )
    result = tx.commit()
    print(result.status, result.calls)
"""

from __future__ import annotations

from typing import Any, Tuple, Union  # noqa: UP035

from ._http import HTTPClient
from .exceptions import TransactionFailed, TransactionNotFound
from .models import (
    CompensationSpec,
    Transaction,
    TransactionCall,
    TransactionResult,
)

# A compensation can be expressed in either of these equivalent forms:
#
#   1. A ``(tool_id, arguments_template)`` tuple — concise and ergonomic.
#   2. A :class:`CompensationSpec` instance — explicit when callers need
#      to inspect or pass around the spec.
#   3. A plain dict matching ``{"tool_id": ..., "arguments_template": ...}``.
#   4. ``None`` — nothing to undo.
#
# We use ``typing.Union`` (instead of PEP-604 ``|``) because this annotation
# is *also* used as a runtime value (in :func:`_coerce_compensation`) and
# Python 3.9 can't evaluate ``X | Y`` between a parametrised generic and a
# non-typing class at runtime.
CompensationLike = Union[
    Tuple[str, "dict[str, Any]"], CompensationSpec, "dict[str, Any]", None
]


def _coerce_compensation(comp: CompensationLike) -> CompensationSpec | None:
    """Normalise the various accepted compensation shapes."""
    if comp is None:
        return None
    if isinstance(comp, CompensationSpec):
        return comp
    if isinstance(comp, tuple):
        if len(comp) != 2:
            raise ValueError(
                f"compensation tuple must be (tool_id, args_template); got {comp!r}"
            )
        tool_id, template = comp
        return CompensationSpec(tool_id=tool_id, arguments_template=dict(template or {}))
    if isinstance(comp, dict):
        return CompensationSpec.model_validate(comp)
    raise TypeError(
        "compensation must be None, a (tool_id, dict) tuple, "
        "a CompensationSpec, or a dict — got "
        f"{type(comp).__name__}"
    )


# ---------------------------------------------------------------------------
# Transactions client (CRUD over the gateway endpoints)
# ---------------------------------------------------------------------------


class TransactionsClient:
    """CRUD client for the gateway's ``/v1/transactions`` resources.

    The client itself only handles the wire protocol; the Saga semantics
    live entirely in the gateway. Most users go through
    :class:`TransactionBuilder` (returned by ``client.gateway.transaction(...)``)
    instead of touching this class directly.
    """

    def __init__(self, http: HTTPClient) -> None:
        self._http = http

    # ----- low-level wire calls --------------------------------------------

    def create(
        self,
        *,
        workspace_id: str | None = None,
        agent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Transaction:
        """Create a new pending transaction."""
        body: dict[str, Any] = {}
        if workspace_id is not None:
            body["workspace_id"] = workspace_id
        if agent_id is not None:
            body["agent_id"] = agent_id
        if metadata is not None:
            body["metadata"] = metadata
        response = self._http.post("/v1/transactions", json=body)
        return Transaction.model_validate(response.json())

    def add_call(
        self,
        tx_id: str,
        tool_id: str,
        arguments: dict[str, Any] | None = None,
        *,
        compensation: CompensationLike = None,
    ) -> TransactionCall:
        """Append one call to a pending transaction."""
        body: dict[str, Any] = {
            "tool_id": tool_id,
            "arguments": arguments or {},
        }
        spec = _coerce_compensation(compensation)
        if spec is not None:
            body["compensation"] = spec.model_dump(mode="json")
        response = self._http.post(
            f"/v1/transactions/{tx_id}/calls",
            json=body,
            not_found_class=TransactionNotFound,
        )
        return TransactionCall.model_validate(response.json())

    def commit(self, tx_id: str) -> TransactionResult:
        """Commit a transaction. Maps catastrophic 5xx to TransactionFailed."""
        try:
            response = self._http.post(
                f"/v1/transactions/{tx_id}/commit",
                not_found_class=TransactionNotFound,
            )
        except TransactionNotFound:
            raise
        except Exception as exc:
            # Wrap unexpected catastrophic errors so app code has a single
            # exception type to catch beyond the structured rolled_back path.
            from .exceptions import PlinthError

            if isinstance(exc, PlinthError):
                # Pass-through — the underlying class is already specific.
                raise
            raise TransactionFailed(
                f"transaction commit failed catastrophically: {exc}",
            ) from exc
        return TransactionResult.model_validate(response.json())

    def rollback(self, tx_id: str) -> TransactionResult:
        """Manually roll a transaction back (compensations only)."""
        response = self._http.post(
            f"/v1/transactions/{tx_id}/rollback",
            not_found_class=TransactionNotFound,
        )
        return TransactionResult.model_validate(response.json())

    def get(self, tx_id: str) -> Transaction:
        """Fetch one transaction (with its calls)."""
        data = self._http.get_json(
            f"/v1/transactions/{tx_id}",
            not_found_class=TransactionNotFound,
        )
        return Transaction.model_validate(data)

    def list(
        self,
        *,
        workspace_id: str | None = None,
        status: str | None = None,
        limit: int | None = None,
    ) -> list[Transaction]:
        """List transactions, optionally filtering by workspace + status."""
        params: dict[str, Any] = {}
        if workspace_id is not None:
            params["workspace_id"] = workspace_id
        if status is not None:
            params["status"] = status
        if limit is not None:
            params["limit"] = limit
        data = self._http.get_json("/v1/transactions", params=params)
        return [Transaction.model_validate(t) for t in data.get("transactions", [])]

    def delete(self, tx_id: str) -> None:
        """Delete a pending or rolled-back transaction.

        Raises :class:`~plinth.exceptions.TransactionInvalidStatus` if the
        transaction is in a terminal-committed state.
        """
        self._http.delete(
            f"/v1/transactions/{tx_id}",
            not_found_class=TransactionNotFound,
        )


# ---------------------------------------------------------------------------
# Builder — fluent surface
# ---------------------------------------------------------------------------


class TransactionBuilder:
    """Fluent builder around a single transaction resource.

    Returned by ``client.gateway.transaction(...)``. Each call to
    :meth:`add` reaches the gateway with one HTTP POST, so users can
    inspect call ids / seqs immediately after adding.
    """

    def __init__(
        self,
        client: TransactionsClient,
        *,
        workspace_id: str | None = None,
        agent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._client = client
        self._tx = client.create(
            workspace_id=workspace_id,
            agent_id=agent_id,
            metadata=metadata,
        )

    # ----- accessors -------------------------------------------------------

    @property
    def id(self) -> str:
        """The server-assigned transaction id (``tx_<ulid>``)."""
        return self._tx.id

    @property
    def transaction(self) -> Transaction:
        """The most recent server-side view of this transaction."""
        return self._tx

    # ----- mutations -------------------------------------------------------

    def add(
        self,
        tool_id: str,
        arguments: dict[str, Any] | None = None,
        *,
        compensation: CompensationLike = None,
    ) -> TransactionCall:
        """Append a call to the transaction.

        ``compensation`` may be:

        * ``None`` — nothing to undo for this call;
        * a ``(tool_id, args_template)`` tuple — most concise;
        * a :class:`~plinth.models.CompensationSpec` — explicit;
        * a dict matching the spec shape.

        Templates may reference earlier results via ``{seq.<n>.result.<field>}``
        or, in the compensation, ``{result.<field>}`` for the forward
        call's own result.
        """
        call = self._client.add_call(
            self._tx.id,
            tool_id,
            arguments,
            compensation=compensation,
        )
        # Keep the local mirror in sync so callers can iterate over .transaction
        self._tx.calls.append(call)
        return call

    def commit(self) -> TransactionResult:
        """Commit the transaction. See module docstring."""
        result = self._client.commit(self._tx.id)
        # Refresh the local cache with the post-commit canonical state.
        self._tx = self._client.get(self._tx.id)
        return result

    def rollback(self) -> TransactionResult:
        """Manually roll the transaction back without committing."""
        result = self._client.rollback(self._tx.id)
        self._tx = self._client.get(self._tx.id)
        return result

    def refresh(self) -> Transaction:
        """Reload the server's view of this transaction."""
        self._tx = self._client.get(self._tx.id)
        return self._tx


__all__ = [
    "CompensationLike",
    "TransactionBuilder",
    "TransactionsClient",
]
