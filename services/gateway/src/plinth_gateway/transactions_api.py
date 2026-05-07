# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""FastAPI router for the gateway's Workflow Transactions API.

The router wires :class:`TransactionStore` and :class:`TransactionEngine`
to the seven HTTP endpoints documented in CONTRACTS.md (v0.5).

All dependencies are pulled off ``request.app.state`` so the router is
factoryable and tests can configure the dependency graph without DI
tooling.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Query, Request, Response

from .auth import check_inbound_auth
from .exceptions import TransactionInvalidStatus
from .logging_config import get_logger
from .models import (
    Transaction,
    TransactionCall,
    TransactionCallAdd,
    TransactionCreate,
    TransactionListResponse,
    TransactionResult,
)
from .transactions import (
    TransactionEngine,
    TransactionStore,
    _EngineDeps,
)

log = get_logger(__name__)


def create_transactions_router() -> APIRouter:
    """Build the ``/v1/transactions/...`` router."""
    router = APIRouter(prefix="/v1/transactions", tags=["transactions"])

    async def _inbound_auth(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> None:
        if request.app.state.settings.inbound_auth_required:
            check_inbound_auth(authorization)

    def _scope_tenant(request: Request) -> str | None:
        settings = request.app.state.settings
        if settings.auth_mode == "permissive":
            return None
        return getattr(request.state, "tenant_id", "default")

    def _request_tenant(request: Request) -> str:
        """Tenant under which a *new* resource is created."""
        return getattr(request.state, "tenant_id", "default") or "default"

    def _store(request: Request) -> TransactionStore:
        store = getattr(request.app.state, "transaction_store", None)
        if store is None:
            store = TransactionStore(request.app.state.db)
            request.app.state.transaction_store = store
        return store

    def _engine(request: Request) -> TransactionEngine:
        store = _store(request)
        deps = _EngineDeps(
            registry=request.app.state.registry,
            proxy=request.app.state.proxy,
            cache=request.app.state.cache,
            audit=request.app.state.audit,
            limits=request.app.state.limits,
            settings=request.app.state.settings,
            oauth_connections=getattr(
                request.app.state, "oauth_connections", None
            ),
        )
        return TransactionEngine(store, deps)

    # ------------------------------------------------------------------
    # Create transaction
    # ------------------------------------------------------------------

    @router.post(
        "",
        response_model=Transaction,
        status_code=201,
        dependencies=[Depends(_inbound_auth)],
    )
    async def create_transaction(
        body: TransactionCreate,
        request: Request,
    ) -> Transaction:
        store = _store(request)
        return await store.create(
            workspace_id=body.workspace_id,
            agent_id=body.agent_id,
            tenant_id=_request_tenant(request),
            metadata=body.metadata,
        )

    # ------------------------------------------------------------------
    # Add call
    # ------------------------------------------------------------------

    @router.post(
        "/{tx_id}/calls",
        response_model=TransactionCall,
        status_code=201,
        dependencies=[Depends(_inbound_auth)],
    )
    async def add_call(
        tx_id: str,
        body: TransactionCallAdd,
        request: Request,
    ) -> TransactionCall:
        store = _store(request)
        tenant = _scope_tenant(request)
        tx = await store.get(tx_id, tenant_id=tenant)

        if tx.status != "pending":
            raise TransactionInvalidStatus(
                f"cannot add call to transaction in status {tx.status!r}",
                details={"tx_id": tx_id, "status": tx.status},
            )
        seq = await store.next_seq(tx_id)
        return await store.add_call(
            tx_id=tx_id,
            seq=seq,
            tool_id=body.tool_id,
            arguments=body.arguments,
            compensation=body.compensation,
        )

    # ------------------------------------------------------------------
    # Commit
    # ------------------------------------------------------------------

    @router.post(
        "/{tx_id}/commit",
        response_model=TransactionResult,
        dependencies=[Depends(_inbound_auth)],
    )
    async def commit(
        tx_id: str,
        request: Request,
    ) -> TransactionResult:
        engine = _engine(request)
        return await engine.commit(tx_id, tenant_id=_scope_tenant(request))

    # ------------------------------------------------------------------
    # Rollback
    # ------------------------------------------------------------------

    @router.post(
        "/{tx_id}/rollback",
        response_model=TransactionResult,
        dependencies=[Depends(_inbound_auth)],
    )
    async def rollback(
        tx_id: str,
        request: Request,
    ) -> TransactionResult:
        engine = _engine(request)
        return await engine.rollback(tx_id, tenant_id=_scope_tenant(request))

    # ------------------------------------------------------------------
    # Get one
    # ------------------------------------------------------------------

    @router.get(
        "/{tx_id}",
        response_model=Transaction,
        dependencies=[Depends(_inbound_auth)],
    )
    async def get_transaction(
        tx_id: str,
        request: Request,
    ) -> Transaction:
        store = _store(request)
        return await store.get(tx_id, tenant_id=_scope_tenant(request))

    # ------------------------------------------------------------------
    # List
    # ------------------------------------------------------------------

    @router.get(
        "",
        response_model=TransactionListResponse,
        dependencies=[Depends(_inbound_auth)],
    )
    async def list_transactions(
        request: Request,
        workspace_id: str | None = Query(default=None),
        status: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> TransactionListResponse:
        store = _store(request)
        rows = await store.list(
            workspace_id=workspace_id,
            status=status,
            tenant_id=_scope_tenant(request),
            limit=limit,
        )
        return TransactionListResponse(transactions=rows)

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    @router.delete(
        "/{tx_id}",
        status_code=204,
        dependencies=[Depends(_inbound_auth)],
    )
    async def delete_transaction(
        tx_id: str,
        request: Request,
    ) -> Response:
        store = _store(request)
        tenant = _scope_tenant(request)
        tx = await store.get(tx_id, tenant_id=tenant)
        if tx.status not in {"pending", "rolled_back"}:
            raise TransactionInvalidStatus(
                f"cannot delete transaction in status {tx.status!r}",
                details={"tx_id": tx_id, "status": tx.status},
            )
        await store.delete(tx_id)
        return Response(status_code=204)

    return router


__all__ = ["create_transactions_router"]
