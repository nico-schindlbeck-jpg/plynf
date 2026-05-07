# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Workflow Transactions — Saga-style commit/compensate over tool calls.

A transaction groups a sequence of tool calls into a single atomic unit.
On commit, calls execute in ``seq`` order through the existing
``/v1/invoke`` machinery (audit, cache, OAuth, rate limits). On
mid-flight failure, already-committed calls have their registered
compensations invoked in reverse order — the Saga pattern.

Two collaborating types live here:

* :class:`TransactionStore` — pure persistence over the ``transactions``
  and ``transaction_calls`` SQLite tables.
* :class:`TransactionEngine` — orchestrates commit / rollback semantics
  (status transitions, compensation cascade, argument templating).

The engine uses the existing :class:`~plinth_gateway.proxy.HttpProxy`,
:class:`~plinth_gateway.cache.Cache`, :class:`~plinth_gateway.audit.AuditLog`
and :class:`~plinth_gateway.limits.LimitsRegistry` so each transaction
call is treated identically to a one-shot ``/v1/invoke`` request.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ulid import ULID

from .audit import AuditLog, AuditRecord
from .cache import Cache, hash_args, hash_result
from .db import Database
from .exceptions import (
    GatewayError,
    ToolNotFound,
    TransactionInvalidStatus,
    TransactionNotFound,
    TransactionRenderError,
)
from .logging_config import get_logger
from .models import (
    CompensationSpec,
    Transaction,
    TransactionCall,
    TransactionResult,
)
from .pricing import estimate_cost
from .proxy import HttpProxy
from .registry import Registry

log = get_logger(__name__)


# Sentinel for keyword-only arguments that *should not* be touched when the
# caller omits them. ``None`` is a valid value for several columns (e.g. you
# may legitimately want to clear ``error``), so we can't use ``None`` as the
# default.
_UNSET: Any = object()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value))


def new_tx_id() -> str:
    """Return a fresh ``tx_<ulid>`` identifier."""
    return f"tx_{ULID()}"


def new_txc_id() -> str:
    """Return a fresh ``txc_<ulid>`` identifier."""
    return f"txc_{ULID()}"


# ---------------------------------------------------------------------------
# Argument template rendering
# ---------------------------------------------------------------------------


# A placeholder is an entire string of the form ``{result.foo.bar}``,
# ``{seq.0.result.url}``, or a substring containing one. We support both
# *whole-string substitution* (preserving the value's type — int, dict, etc.)
# and *interpolation inside a larger string*. Patterns match either form.
_PLACEHOLDER_RE = re.compile(
    r"\{(?P<expr>(?:seq\.\d+\.)?result(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\}"
)
_WHOLE_PLACEHOLDER_RE = re.compile(
    r"^\{(?P<expr>(?:seq\.\d+\.)?result(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\}$"
)


def _resolve_path(
    expr: str,
    *,
    prior_calls: list[TransactionCall],
    forward_result: Any | None = None,
) -> Any:
    """Walk ``expr`` against the prior call results.

    ``expr`` examples (after the leading ``{`` / trailing ``}`` are stripped):

    * ``"result.foo.bar"`` — refers to the most recently committed call's
      result, or to ``forward_result`` when one is supplied (compensation
      rendering).
    * ``"seq.0.result.url"`` — refers to the call at ``seq=0``.

    Raises :class:`TransactionRenderError` on a missing reference.
    """
    parts = expr.split(".")
    if not parts:
        raise TransactionRenderError(
            f"empty placeholder expression: {expr!r}",
            details={"expr": expr},
        )

    # ``seq.N.result...`` form.
    if parts[0] == "seq":
        if len(parts) < 3 or parts[2] != "result":
            raise TransactionRenderError(
                f"invalid placeholder shape: {expr!r}",
                details={"expr": expr},
            )
        try:
            seq_num = int(parts[1])
        except ValueError as exc:
            raise TransactionRenderError(
                f"seq index must be int in {expr!r}",
                details={"expr": expr},
            ) from exc

        target: TransactionCall | None = None
        for call in prior_calls:
            if call.seq == seq_num:
                target = call
                break
        if target is None:
            raise TransactionRenderError(
                f"no committed call with seq={seq_num}",
                details={"expr": expr, "seq": seq_num},
            )
        value: Any = target.result
        path = parts[3:]
    elif parts[0] == "result":
        # Most recent committed call by default; otherwise compensations
        # supply ``forward_result`` directly.
        if forward_result is not None:
            value = forward_result
        else:
            if not prior_calls:
                raise TransactionRenderError(
                    "{result.*} with no prior committed calls",
                    details={"expr": expr},
                )
            value = prior_calls[-1].result
        path = parts[1:]
    else:
        raise TransactionRenderError(
            f"placeholder must start with 'seq' or 'result': {expr!r}",
            details={"expr": expr},
        )

    for segment in path:
        if value is None:
            raise TransactionRenderError(
                f"cannot traverse {segment!r} on null in {expr!r}",
                details={"expr": expr, "segment": segment},
            )
        if isinstance(value, dict):
            if segment not in value:
                raise TransactionRenderError(
                    f"missing field {segment!r} in {expr!r}",
                    details={"expr": expr, "segment": segment},
                )
            value = value[segment]
        else:
            raise TransactionRenderError(
                f"cannot traverse {segment!r} on non-dict in {expr!r}",
                details={"expr": expr, "segment": segment, "kind": type(value).__name__},
            )
    return value


def _render_value(
    value: Any,
    *,
    prior_calls: list[TransactionCall],
    forward_result: Any | None = None,
) -> Any:
    """Recursively substitute placeholders in ``value``.

    Type preservation rule: when an entire string is a single placeholder,
    we substitute with the *raw value* (so ``{result.number}`` returns an
    int when the result has an integer field). When the placeholder is
    embedded in a larger string, we substitute its string representation
    in-place.
    """
    if isinstance(value, str):
        whole = _WHOLE_PLACEHOLDER_RE.match(value)
        if whole is not None:
            return _resolve_path(
                whole.group("expr"),
                prior_calls=prior_calls,
                forward_result=forward_result,
            )

        # Otherwise substitute every embedded placeholder by its string form.
        def _sub(match: re.Match[str]) -> str:
            resolved = _resolve_path(
                match.group("expr"),
                prior_calls=prior_calls,
                forward_result=forward_result,
            )
            if isinstance(resolved, (dict, list)):
                return json.dumps(resolved, separators=(",", ":"))
            return str(resolved)

        return _PLACEHOLDER_RE.sub(_sub, value)

    if isinstance(value, dict):
        return {
            k: _render_value(v, prior_calls=prior_calls, forward_result=forward_result)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [
            _render_value(item, prior_calls=prior_calls, forward_result=forward_result)
            for item in value
        ]
    return value


def render_arguments(
    arguments: dict[str, Any],
    prior_calls: list[TransactionCall],
) -> dict[str, Any]:
    """Render forward-call arguments. Placeholders reference earlier calls."""
    rendered = _render_value(arguments, prior_calls=prior_calls)
    if not isinstance(rendered, dict):
        # Defensive: input was a dict; output must be a dict.
        raise TransactionRenderError(
            "rendered arguments must remain a dict",
            details={"got": type(rendered).__name__},
        )
    return rendered


def render_compensation_arguments(
    spec: CompensationSpec,
    forward_result: Any,
    prior_calls: list[TransactionCall],
) -> dict[str, Any]:
    """Render compensation arguments using the forward call's result."""
    rendered = _render_value(
        spec.arguments_template,
        prior_calls=prior_calls,
        forward_result=forward_result,
    )
    if not isinstance(rendered, dict):
        raise TransactionRenderError(
            "rendered compensation arguments must remain a dict",
            details={"got": type(rendered).__name__},
        )
    return rendered


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _row_to_call(row: Any) -> TransactionCall:
    comp_spec = None
    raw_comp = row["compensation_spec"]
    if raw_comp:
        comp_spec = CompensationSpec.model_validate(json.loads(raw_comp))
    raw_result = row["result"]
    return TransactionCall(
        id=row["id"],
        tx_id=row["tx_id"],
        seq=int(row["seq"]),
        tool_id=row["tool_id"],
        arguments=json.loads(row["arguments"]),
        compensation=comp_spec,
        status=row["status"],
        result=json.loads(raw_result) if raw_result is not None else None,
        error=row["error"],
        invoked_at=_parse_ts(row["invoked_at"]),
        finished_at=_parse_ts(row["finished_at"]),
    )


def _row_to_tx(row: Any, calls: list[TransactionCall]) -> Transaction:
    return Transaction(
        id=row["id"],
        status=row["status"],
        workspace_id=row["workspace_id"],
        agent_id=row["agent_id"],
        tenant_id=row["tenant_id"] or "default",
        metadata=json.loads(row["metadata"]) if row["metadata"] else {},
        calls=calls,
        created_at=_parse_ts(row["created_at"]),
        committed_at=_parse_ts(row["committed_at"]),
        rolled_back_at=_parse_ts(row["rolled_back_at"]),
    )


class TransactionStore:
    """Pure persistence layer over the transactions tables.

    Knows nothing about commit semantics — it just writes and reads rows.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    # ----- create / fetch --------------------------------------------------

    async def create(
        self,
        *,
        workspace_id: str | None,
        agent_id: str | None,
        tenant_id: str,
        metadata: dict[str, Any] | None,
    ) -> Transaction:
        tx_id = new_tx_id()
        now = _utcnow()
        meta = metadata or {}
        await self._db.execute(
            """
            INSERT INTO transactions
              (id, workspace_id, agent_id, tenant_id, status, metadata, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                tx_id,
                workspace_id,
                agent_id,
                tenant_id,
                json.dumps(meta),
                now.isoformat(),
            ),
        )
        return Transaction(
            id=tx_id,
            status="pending",
            workspace_id=workspace_id,
            agent_id=agent_id,
            tenant_id=tenant_id,
            metadata=meta,
            calls=[],
            created_at=now,
            committed_at=None,
            rolled_back_at=None,
        )

    async def get(
        self,
        tx_id: str,
        *,
        tenant_id: str | None = None,
    ) -> Transaction:
        row = await self._db.fetchone(
            "SELECT * FROM transactions WHERE id = ?", (tx_id,)
        )
        if row is None:
            raise TransactionNotFound(
                f"Transaction {tx_id!r} does not exist",
                details={"tx_id": tx_id},
            )
        if tenant_id is not None and (row["tenant_id"] or "default") != tenant_id:
            # Hide cross-tenant transactions behind a 404 — same shape used
            # by the tools registry for cross-tenant tool lookups.
            raise TransactionNotFound(
                f"Transaction {tx_id!r} does not exist",
                details={"tx_id": tx_id},
            )

        call_rows = await self._db.fetchall(
            "SELECT * FROM transaction_calls WHERE tx_id = ? ORDER BY seq ASC",
            (tx_id,),
        )
        calls = [_row_to_call(r) for r in call_rows]
        return _row_to_tx(row, calls)

    async def list(
        self,
        *,
        workspace_id: str | None = None,
        status: str | None = None,
        tenant_id: str | None = None,
        limit: int = 100,
    ) -> list[Transaction]:
        clauses: list[str] = []
        params: list[Any] = []
        if workspace_id is not None:
            clauses.append("workspace_id = ?")
            params.append(workspace_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT * FROM transactions {where} "
            "ORDER BY created_at DESC LIMIT ?"
        )
        params.append(limit)
        rows = await self._db.fetchall(sql, tuple(params))

        out: list[Transaction] = []
        for row in rows:
            call_rows = await self._db.fetchall(
                "SELECT * FROM transaction_calls WHERE tx_id = ? ORDER BY seq ASC",
                (row["id"],),
            )
            calls = [_row_to_call(r) for r in call_rows]
            out.append(_row_to_tx(row, calls))
        return out

    # ----- mutations -------------------------------------------------------

    async def update_status(
        self,
        tx_id: str,
        status: str,
        *,
        committed_at: datetime | None = None,
        rolled_back_at: datetime | None = None,
    ) -> None:
        sets = ["status = ?"]
        params: list[Any] = [status]
        if committed_at is not None:
            sets.append("committed_at = ?")
            params.append(committed_at.isoformat())
        if rolled_back_at is not None:
            sets.append("rolled_back_at = ?")
            params.append(rolled_back_at.isoformat())
        params.append(tx_id)
        await self._db.execute(
            f"UPDATE transactions SET {', '.join(sets)} WHERE id = ?",
            tuple(params),
        )

    async def delete(self, tx_id: str) -> None:
        # Children first so the foreign key check stays clean even if the
        # journal shows individual statements separately.
        await self._db.execute(
            "DELETE FROM transaction_calls WHERE tx_id = ?", (tx_id,)
        )
        await self._db.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))

    async def add_call(
        self,
        *,
        tx_id: str,
        seq: int,
        tool_id: str,
        arguments: dict[str, Any],
        compensation: CompensationSpec | None,
    ) -> TransactionCall:
        call_id = new_txc_id()
        comp_json = (
            json.dumps(compensation.model_dump(mode="json"))
            if compensation is not None
            else None
        )
        await self._db.execute(
            """
            INSERT INTO transaction_calls
              (id, tx_id, seq, tool_id, arguments, compensation_spec, status)
            VALUES (?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                call_id,
                tx_id,
                seq,
                tool_id,
                json.dumps(arguments),
                comp_json,
            ),
        )
        return TransactionCall(
            id=call_id,
            tx_id=tx_id,
            seq=seq,
            tool_id=tool_id,
            arguments=arguments,
            compensation=compensation,
            status="pending",
        )

    async def update_call(
        self,
        call_id: str,
        *,
        status: str | None = None,
        result: Any = _UNSET,
        error: Any = _UNSET,
        arguments: dict[str, Any] | None = None,
        invoked_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> None:
        sets: list[str] = []
        params: list[Any] = []
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if result is not _UNSET:
            sets.append("result = ?")
            params.append(json.dumps(result) if result is not None else None)
        if error is not _UNSET:
            sets.append("error = ?")
            params.append(error)
        if arguments is not None:
            sets.append("arguments = ?")
            params.append(json.dumps(arguments))
        if invoked_at is not None:
            sets.append("invoked_at = ?")
            params.append(invoked_at.isoformat())
        if finished_at is not None:
            sets.append("finished_at = ?")
            params.append(finished_at.isoformat())
        if not sets:
            return
        params.append(call_id)
        await self._db.execute(
            f"UPDATE transaction_calls SET {', '.join(sets)} WHERE id = ?",
            tuple(params),
        )

    async def next_seq(self, tx_id: str) -> int:
        """Return the next monotonic seq for ``tx_id`` (0-indexed)."""
        row = await self._db.fetchone(
            "SELECT MAX(seq) AS m FROM transaction_calls WHERE tx_id = ?",
            (tx_id,),
        )
        if row is None or row["m"] is None:
            return 0
        return int(row["m"]) + 1


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


@dataclass
class _EngineDeps:
    """Bundle of collaborators the engine needs to invoke a tool.

    Carrying these as a dataclass keeps the constructor short and means
    the API router can hand the engine a single ``app.state``-backed
    bundle without leaking each name into the engine's signature.
    """

    registry: Registry
    proxy: HttpProxy
    cache: Cache
    audit: AuditLog
    limits: Any  # LimitsRegistry — typed loosely to avoid the import cycle
    settings: Any
    oauth_connections: Any | None = None


class TransactionEngine:
    """Saga-style commit/compensate engine.

    The engine is instantiated per request, holding an ``_EngineDeps`` bundle
    that wires it up to the gateway's existing primitives. This means
    transactions inherit, for free:

    * audit-log entries per call (forward + compensation)
    * cache lookups for idempotent tools
    * rate-limit + cost-cap enforcement
    * OAuth resolution for ``oauth2`` tools
    """

    def __init__(
        self,
        store: TransactionStore,
        deps: _EngineDeps,
    ) -> None:
        self._store = store
        self._deps = deps

    # ----- public API ------------------------------------------------------

    async def commit(self, tx_id: str, *, tenant_id: str | None) -> TransactionResult:
        """Execute every call in seq order; compensate on partial failure.

        Idempotent on already-terminal transactions: a second call to a
        ``committed``/``rolled_back``/``failed`` transaction returns the
        same materialised result without re-running anything.
        """
        tx = await self._store.get(tx_id, tenant_id=tenant_id)

        if tx.status in {"committed", "rolled_back", "failed"}:
            # Idempotent: just rebuild the result envelope.
            comps_run = sum(1 for c in tx.calls if c.status == "compensated")
            return TransactionResult(
                tx_id=tx.id,
                status=tx.status,
                calls=tx.calls,
                compensations_run=comps_run,
            )

        if tx.status != "pending":
            raise TransactionInvalidStatus(
                f"cannot commit transaction in status {tx.status!r}",
                details={"tx_id": tx.id, "status": tx.status},
            )

        await self._store.update_status(tx.id, "committing")

        committed_calls: list[TransactionCall] = []
        failed_call: TransactionCall | None = None

        sorted_calls = sorted(tx.calls, key=lambda c: c.seq)
        for call in sorted_calls:
            try:
                rendered_args = render_arguments(call.arguments, committed_calls)
            except TransactionRenderError as exc:
                await self._store.update_call(
                    call.id,
                    status="failed",
                    error=f"render error: {exc.message}",
                    finished_at=_utcnow(),
                )
                call.status = "failed"
                call.error = f"render error: {exc.message}"
                failed_call = call
                break

            await self._store.update_call(
                call.id,
                status="running",
                arguments=rendered_args,
                invoked_at=_utcnow(),
            )

            try:
                result = await self._invoke_tool(
                    tool_id=call.tool_id,
                    arguments=rendered_args,
                    workspace_id=tx.workspace_id,
                    agent_id=tx.agent_id,
                    tenant_id=tx.tenant_id,
                )
            except GatewayError as exc:
                err = exc.message
                await self._store.update_call(
                    call.id,
                    status="failed",
                    error=err,
                    finished_at=_utcnow(),
                )
                call.status = "failed"
                call.error = err
                call.arguments = rendered_args
                failed_call = call
                log.warning(
                    "tx.call.failed",
                    tx_id=tx.id,
                    call_id=call.id,
                    tool_id=call.tool_id,
                    error=err,
                )
                break

            await self._store.update_call(
                call.id,
                status="committed",
                result=result,
                finished_at=_utcnow(),
            )
            call.status = "committed"
            call.result = result
            call.arguments = rendered_args
            committed_calls.append(call)

        if failed_call is None:
            await self._store.update_status(
                tx.id, "committed", committed_at=_utcnow()
            )
            log.info(
                "tx.committed",
                tx_id=tx.id,
                calls=len(committed_calls),
            )
            return TransactionResult(
                tx_id=tx.id,
                status="committed",
                calls=committed_calls,
                compensations_run=0,
            )

        # Failure path: compensate.
        await self._store.update_status(tx.id, "compensating")
        comps_run = await self._run_compensations(
            committed_calls,
            tenant_id=tx.tenant_id,
            workspace_id=tx.workspace_id,
            agent_id=tx.agent_id,
            tx_id=tx.id,
        )
        await self._store.update_status(
            tx.id, "rolled_back", rolled_back_at=_utcnow()
        )
        log.info(
            "tx.rolled_back",
            tx_id=tx.id,
            committed=len(committed_calls),
            compensations_run=comps_run,
        )
        # Return the merged-in-memory list so callers see compensated calls.
        refreshed = await self._store.get(tx.id, tenant_id=tenant_id)
        return TransactionResult(
            tx_id=tx.id,
            status="rolled_back",
            calls=refreshed.calls,
            compensations_run=comps_run,
        )

    async def rollback(
        self, tx_id: str, *, tenant_id: str | None
    ) -> TransactionResult:
        """Manual rollback / abort path.

        * ``pending`` — no calls have run; just mark ``rolled_back``.
        * ``committing`` — partial commit; compensate already-committed
          calls.
        * ``committed`` — refuse: caller must use a separate undo
          transaction.
        * Already terminal — idempotent (same shape as :meth:`commit`).
        """
        tx = await self._store.get(tx_id, tenant_id=tenant_id)

        if tx.status == "rolled_back":
            comps_run = sum(1 for c in tx.calls if c.status == "compensated")
            return TransactionResult(
                tx_id=tx.id,
                status="rolled_back",
                calls=tx.calls,
                compensations_run=comps_run,
            )

        if tx.status == "committed":
            raise TransactionInvalidStatus(
                "cannot rollback a committed transaction; "
                "submit a separate undo transaction instead",
                details={"tx_id": tx.id, "status": tx.status},
            )
        if tx.status == "failed":
            raise TransactionInvalidStatus(
                f"cannot rollback transaction in status {tx.status!r}",
                details={"tx_id": tx.id, "status": tx.status},
            )

        if tx.status == "pending":
            await self._store.update_status(
                tx.id, "rolled_back", rolled_back_at=_utcnow()
            )
            log.info("tx.rolled_back.empty", tx_id=tx.id)
            refreshed = await self._store.get(tx.id, tenant_id=tenant_id)
            return TransactionResult(
                tx_id=tx.id,
                status="rolled_back",
                calls=refreshed.calls,
                compensations_run=0,
            )

        # status == "committing" or "compensating": compensate the already-
        # committed calls.
        committed_calls = [c for c in tx.calls if c.status == "committed"]
        await self._store.update_status(tx.id, "compensating")
        comps_run = await self._run_compensations(
            committed_calls,
            tenant_id=tx.tenant_id,
            workspace_id=tx.workspace_id,
            agent_id=tx.agent_id,
            tx_id=tx.id,
        )
        await self._store.update_status(
            tx.id, "rolled_back", rolled_back_at=_utcnow()
        )
        refreshed = await self._store.get(tx.id, tenant_id=tenant_id)
        return TransactionResult(
            tx_id=tx.id,
            status="rolled_back",
            calls=refreshed.calls,
            compensations_run=comps_run,
        )

    # ----- internals -------------------------------------------------------

    async def _run_compensations(
        self,
        committed_calls: list[TransactionCall],
        *,
        tenant_id: str,
        workspace_id: str | None,
        agent_id: str | None,
        tx_id: str,
    ) -> int:
        """Run compensations in reverse seq order. Best-effort.

        A compensation that itself fails is logged and recorded on the
        call's ``error`` field, but does not abort other compensations.
        """
        count = 0
        for call in reversed(committed_calls):
            spec = call.compensation
            if spec is None:
                # Nothing to undo — skip without bumping count or status.
                continue

            await self._store.update_call(call.id, status="compensating")
            try:
                comp_args = render_compensation_arguments(
                    spec, call.result, prior_calls=committed_calls
                )
            except TransactionRenderError as exc:
                msg = f"compensation render error: {exc.message}"
                await self._store.update_call(
                    call.id,
                    error=msg,
                    finished_at=_utcnow(),
                )
                log.error(
                    "tx.compensation.render_failed",
                    tx_id=tx_id,
                    call_id=call.id,
                    tool_id=spec.tool_id,
                    error=msg,
                )
                continue

            try:
                await self._invoke_tool(
                    tool_id=spec.tool_id,
                    arguments=comp_args,
                    workspace_id=workspace_id,
                    agent_id=agent_id,
                    tenant_id=tenant_id,
                )
            except GatewayError as exc:
                msg = f"compensation failed: {exc.message}"
                await self._store.update_call(
                    call.id,
                    error=msg,
                    finished_at=_utcnow(),
                )
                log.error(
                    "tx.compensation.failed",
                    tx_id=tx_id,
                    call_id=call.id,
                    tool_id=spec.tool_id,
                    error=exc.message,
                )
                continue

            await self._store.update_call(
                call.id,
                status="compensated",
                finished_at=_utcnow(),
            )
            count += 1
        return count

    async def _invoke_tool(
        self,
        *,
        tool_id: str,
        arguments: dict[str, Any],
        workspace_id: str | None,
        agent_id: str | None,
        tenant_id: str,
    ) -> Any:
        """Invoke a tool via the same machinery used by ``/v1/invoke``.

        Each call here writes one audit row and respects rate / cost
        ceilings. Returns the raw backend result so the engine can store
        it in ``transaction_calls.result``.
        """
        deps = self._deps

        # Rate / cost enforcement (same gates as /v1/invoke).
        if deps.settings.rate_limits_enabled and agent_id is not None:
            await deps.limits.assert_within_rate(agent_id)
            await deps.limits.assert_within_cost_caps(agent_id)

        # Tenant-scoped tool lookup. We only enforce when the tx itself
        # is tagged with a non-default tenant — keeps existing permissive
        # demos working unchanged.
        scope_tenant = tenant_id if tenant_id and tenant_id != "default" else None
        try:
            tool = await deps.registry.get(tool_id, tenant_id=scope_tenant)
        except ToolNotFound:
            # Re-raise with a friendlier audit-id-less envelope; the
            # caller (commit loop) will record the failure in the call.
            raise

        args_hash = hash_args(arguments)
        args_preview = AuditLog.make_preview(arguments)

        cache_eligible = (
            tool.idempotent
            and tool.cache_ttl_seconds is not None
            and tool.cache_ttl_seconds > 0
        )

        # Cache lookup
        if cache_eligible:
            hit = await deps.cache.lookup(tool.tool_id, arguments)
            if hit is not None:
                cost = estimate_cost(tool.tool_id, cached=True)
                await deps.audit.record(
                    AuditRecord(
                        tool_id=tool.tool_id,
                        arguments=arguments,
                        workspace_id=workspace_id,
                        agent_id=agent_id,
                        tenant_id=tenant_id,
                        arguments_hash=args_hash,
                        arguments_preview=args_preview,
                        cached=True,
                        duration_ms=0,
                        cost_estimate_usd=cost,
                        result_hash=hash_result(hit.result),
                    )
                )
                return hit.result

        # Backend call
        start = time.perf_counter()
        error_message: str | None = None
        result: Any = None
        try:
            result = await deps.proxy.invoke(
                tool,
                arguments,
                connection_store=deps.oauth_connections,
                settings=deps.settings,
            )
        except GatewayError as exc:
            error_message = exc.message
            duration_ms = int((time.perf_counter() - start) * 1000)
            cost = estimate_cost(tool.tool_id, cached=False)
            await deps.audit.record(
                AuditRecord(
                    tool_id=tool.tool_id,
                    arguments=arguments,
                    workspace_id=workspace_id,
                    agent_id=agent_id,
                    tenant_id=tenant_id,
                    arguments_hash=args_hash,
                    arguments_preview=args_preview,
                    cached=False,
                    duration_ms=duration_ms,
                    cost_estimate_usd=cost,
                    error=error_message,
                )
            )
            raise

        duration_ms = int((time.perf_counter() - start) * 1000)
        cost = estimate_cost(tool.tool_id, cached=False)

        if cache_eligible:
            await deps.cache.store(
                tool.tool_id,
                arguments,
                result,
                ttl_seconds=tool.cache_ttl_seconds or 0,
            )

        await deps.audit.record(
            AuditRecord(
                tool_id=tool.tool_id,
                arguments=arguments,
                workspace_id=workspace_id,
                agent_id=agent_id,
                tenant_id=tenant_id,
                arguments_hash=args_hash,
                arguments_preview=args_preview,
                cached=False,
                duration_ms=duration_ms,
                cost_estimate_usd=cost,
                result_hash=hash_result(result),
            )
        )
        return result


__all__ = [
    "TransactionEngine",
    "TransactionStore",
    "_EngineDeps",
    "render_arguments",
    "render_compensation_arguments",
]
