# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Channel schema store + JSON Schema validator for typed channels (v0.5).

A channel becomes "typed" when a JSON Schema is attached via
``POST .../channels/{name}/schema``. From that point on, every send is
validated; failed payloads are routed to a hidden ``<channel>.deadletter``
sub-channel and the caller receives an HTTP 422 ``SCHEMA_VIOLATION``.

This module is intentionally tiny — it only owns persistence + validation.
Channel I/O lives in :mod:`plinth_workspace.channels`; the two modules
collaborate by composition (see :class:`ChannelStore.set_schema_store`).

The validator uses :mod:`jsonschema` for real spec compliance. Schema
documents themselves are validated for structural correctness on PUT so
operators get an immediate signal rather than discovering a broken schema
the next time a producer sends.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
from jsonschema.exceptions import SchemaError as _JsonSchemaError
from jsonschema.exceptions import ValidationError as _JsonValidationError

from .db import connect, iso, now_utc, parse_ts
from .exceptions import InvalidArguments, WorkspaceNotFound
from .models import ChannelSchema

DEADLETTER_SUFFIX = ".deadletter"


def deadletter_channel_name(channel: str) -> str:
    """Return the hidden DLQ name for ``channel``."""
    return f"{channel}{DEADLETTER_SUFFIX}"


def is_deadletter_channel(name: str) -> bool:
    """True iff ``name`` is a DLQ sub-channel."""
    return name.endswith(DEADLETTER_SUFFIX)


def validate_schema_document(schema_doc: Any) -> None:
    """Raise :class:`InvalidArguments` if ``schema_doc`` is not a valid JSON Schema.

    We use ``jsonschema``'s draft-2020-12 validator (the latest the lib
    supports) for the meta-schema check. Operators can still write older
    drafts (draft-07 etc.) — those are accepted as valid 2020-12 documents
    so we don't gate on the ``$schema`` keyword.
    """

    if not isinstance(schema_doc, dict):
        raise InvalidArguments(
            "channel schema must be a JSON object",
            details={"received_type": type(schema_doc).__name__},
        )
    try:
        jsonschema.Draft202012Validator.check_schema(schema_doc)
    except _JsonSchemaError as exc:
        raise InvalidArguments(
            f"invalid JSON Schema: {exc.message}",
            details={"path": list(exc.path), "validator": exc.validator},
        ) from exc


def format_validation_errors(
    error: _JsonValidationError,
) -> list[dict[str, Any]]:
    """Turn a ``jsonschema.ValidationError`` into a list-of-dicts envelope.

    The list always contains at least one entry — the root error. If the
    validator reported nested ``context`` errors (oneOf / anyOf branches),
    those are appended too so the client can reason about which branch was
    closest to matching.
    """

    out: list[dict[str, Any]] = [
        {
            "message": error.message,
            "path": list(error.absolute_path),
            "validator": error.validator,
        }
    ]
    for sub in error.context or []:
        out.append(
            {
                "message": sub.message,
                "path": list(sub.absolute_path),
                "validator": sub.validator,
            }
        )
    return out


class SchemaStore:
    """Persistence + lookup for channel schemas.

    One row per ``(workspace_id, channel_name)``. PUT bumps the ``version``
    counter monotonically so consumers can detect schema evolution without
    polling for content changes.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    # ------------------------------------------------------------------ helpers

    @staticmethod
    async def _assert_workspace(conn, workspace_id: str) -> None:
        cur = await conn.execute(
            "SELECT 1 FROM workspaces WHERE id=?",
            (workspace_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            raise WorkspaceNotFound(workspace_id)

    @staticmethod
    def _row_to_schema(row) -> ChannelSchema:
        return ChannelSchema(
            workspace_id=row["workspace_id"],
            channel_name=row["channel_name"],
            schema_json=json.loads(row["schema_json"]),
            version=int(row["version"]),
            updated_at=parse_ts(row["updated_at"]),  # type: ignore[arg-type]
        )

    # ------------------------------------------------------------------ CRUD

    async def get(
        self,
        workspace_id: str,
        channel_name: str,
    ) -> ChannelSchema | None:
        """Return the schema attached to ``channel_name`` or ``None``."""

        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)
            cur = await conn.execute(
                "SELECT * FROM channel_schemas "
                "WHERE workspace_id=? AND channel_name=?",
                (workspace_id, channel_name),
            )
            row = await cur.fetchone()
            await cur.close()
            return self._row_to_schema(row) if row is not None else None

    async def set(
        self,
        workspace_id: str,
        channel_name: str,
        schema_doc: dict[str, Any],
    ) -> ChannelSchema:
        """Upsert the channel's schema, bumping ``version`` on each call."""

        validate_schema_document(schema_doc)
        if not channel_name:
            raise InvalidArguments("channel name must be non-empty")
        if is_deadletter_channel(channel_name):
            # The hidden DLQ should never carry its own schema.
            raise InvalidArguments(
                "cannot attach a schema to a deadletter channel",
                details={"channel": channel_name},
            )

        ts = now_utc()
        payload = json.dumps(schema_doc, sort_keys=True)

        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)

            cur = await conn.execute(
                "SELECT version FROM channel_schemas "
                "WHERE workspace_id=? AND channel_name=?",
                (workspace_id, channel_name),
            )
            row = await cur.fetchone()
            await cur.close()
            next_version = int(row["version"]) + 1 if row is not None else 1

            await conn.execute(
                "INSERT INTO channel_schemas "
                "(workspace_id, channel_name, schema_json, version, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(workspace_id, channel_name) DO UPDATE SET "
                "  schema_json=excluded.schema_json, "
                "  version=excluded.version, "
                "  updated_at=excluded.updated_at",
                (workspace_id, channel_name, payload, next_version, iso(ts)),
            )
            await conn.commit()

            return ChannelSchema(
                workspace_id=workspace_id,
                channel_name=channel_name,
                schema_json=schema_doc,
                version=next_version,
                updated_at=ts,
            )

    async def delete(self, workspace_id: str, channel_name: str) -> bool:
        """Remove the schema. Returns True iff a row was deleted."""

        async with connect(self.db_path) as conn:
            await self._assert_workspace(conn, workspace_id)
            cur = await conn.execute(
                "DELETE FROM channel_schemas "
                "WHERE workspace_id=? AND channel_name=?",
                (workspace_id, channel_name),
            )
            deleted = cur.rowcount
            await cur.close()
            await conn.commit()
            return bool(deleted)

    # ------------------------------------------------------------------ validation

    @staticmethod
    def validate(
        payload: Any,
        schema: ChannelSchema | dict[str, Any],
    ) -> list[dict[str, Any]] | None:
        """Validate ``payload`` against ``schema``.

        Returns ``None`` on success; otherwise a list of validation-error
        dicts as produced by :func:`format_validation_errors`.
        """

        schema_doc = schema.schema_json if isinstance(schema, ChannelSchema) else schema
        try:
            jsonschema.validate(payload, schema_doc)
        except _JsonValidationError as exc:
            return format_validation_errors(exc)
        return None


__all__ = [
    "DEADLETTER_SUFFIX",
    "SchemaStore",
    "deadletter_channel_name",
    "format_validation_errors",
    "is_deadletter_channel",
    "validate_schema_document",
]
