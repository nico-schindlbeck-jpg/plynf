# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Output formatting helpers for the CLI.

The CLI supports three output modes — ``human`` (rich tables/colours,
default on a TTY), ``json`` (line-buffered JSON, default when piped),
and ``csv`` (RFC-4180 rows for cheap spreadsheet ingest). All command
modules go through this module instead of touching ``rich`` or ``json``
directly so mode switching is a one-liner at the top of each command.

``human`` and ``table`` are accepted interchangeably for the
``--output``/``--format`` flags so users coming from the spec wording
(``table | json | csv``) see the same behaviour as users following the
README (``human | json``).
"""

from __future__ import annotations

import csv as _csv
import datetime as _dt
import io
import json
import sys
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from rich.console import Console
from rich.table import Table

# Canonical mode set. ``table`` is an alias for ``human`` so the CLI is
# compatible with both vocabulary conventions in the docs.
_MODES = {"human", "json", "csv"}
_ALIASES = {"table": "human"}

# A single Console instance is enough for the CLI's lifetime; rich is
# thread-safe and lazy-detects TTY status automatically.
_console = Console(highlight=False)
_err_console = Console(stderr=True, highlight=False)


# ---------------------------------------------------------------------------
# Mode resolution
# ---------------------------------------------------------------------------


def resolve_mode(cli_flag: str | None, config_value: str) -> str:
    """Pick the effective output mode.

    Args:
        cli_flag: Raw value of ``--output`` / ``--format`` (or ``None``).
        config_value: ``output`` key from the resolved config.

    Returns:
        ``"json"`` if the user asked for it explicitly, the config says
        so, *or* stdout is not a TTY (so piping into jq just works).
        ``"csv"`` if the user asked for it. Otherwise ``"human"``.
    """

    explicit = cli_flag is not None
    raw = (cli_flag or config_value or "human").lower()
    raw = _ALIASES.get(raw, raw)
    if raw not in _MODES:
        raw = "human"
    if raw == "human" and not explicit and not sys.stdout.isatty():
        # Default to JSON when piped — robust to "plinth audit | jq".
        # Skip the auto-flip if the user explicitly asked for human (e.g.
        # piping into ``less -R``).
        return "json"
    return raw


# ---------------------------------------------------------------------------
# Printing primitives
# ---------------------------------------------------------------------------


def emit_json(payload: Any) -> None:
    """Print ``payload`` as a single JSON object (machine-readable)."""

    sys.stdout.write(json.dumps(payload, default=_json_default, sort_keys=True))
    sys.stdout.write("\n")
    sys.stdout.flush()


def emit_human(text: str) -> None:
    """Print rich-formatted text to stdout."""

    _console.print(text)


def emit_table(
    title: str,
    columns: Sequence[str],
    rows: Iterable[Sequence[Any]],
    *,
    caption: str | None = None,
) -> None:
    """Render a rich table to stdout."""

    table = Table(title=title, caption=caption, show_lines=False)
    for c in columns:
        table.add_column(c)
    for row in rows:
        table.add_row(*[_to_cell(v) for v in row])
    _console.print(table)


def emit_csv(
    columns: Sequence[str],
    rows: Iterable[Sequence[Any]],
) -> None:
    """Write rows as CSV to stdout (RFC-4180; ``\\n`` line endings).

    No header row decoration — just ``columns`` first, then each row.
    Cells go through :func:`_to_cell` so dicts/lists become JSON.
    """

    writer = _csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(list(columns))
    for row in rows:
        writer.writerow([_to_cell(v) for v in row])
    sys.stdout.flush()


def emit(
    rows: list[dict[str, Any]],
    headers: Sequence[str],
    *,
    mode: str = "human",
    title: str = "",
) -> None:
    """Single entry point that picks the right formatter for ``mode``.

    The spec calls this out as ``emit(rows, headers, fmt="table")``. We
    keep the per-mode helpers public for callers that already have rich
    table objects, but commands that just have a list-of-dicts can lean
    on this one helper.
    """

    mode = _ALIASES.get(mode.lower(), mode.lower())
    if mode == "json":
        emit_json(rows)
        return
    if mode == "csv":
        emit_csv(headers, [[r.get(h) for h in headers] for r in rows])
        return
    # Default — human/table.
    emit_table(
        title or "",
        list(headers),
        [[r.get(h) for h in headers] for r in rows],
    )


def csv_string(columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    """Return CSV as a string (used by tests + ``plinth tenant export``)."""

    buf = io.StringIO()
    writer = _csv.writer(buf, lineterminator="\n")
    writer.writerow(list(columns))
    for row in rows:
        writer.writerow([_to_cell(v) for v in row])
    return buf.getvalue()


def emit_kv(title: str, kv: dict[str, Any]) -> None:
    """Render a simple two-column key/value table."""

    table = Table(title=title, show_header=False, show_lines=False, box=None)
    table.add_column(style="bold")
    table.add_column()
    for k, v in kv.items():
        table.add_row(k, _to_cell(v))
    _console.print(table)


def emit_warn(message: str) -> None:
    """Print a yellow warning to stderr (does not exit)."""

    _err_console.print(f"[yellow]warning:[/] {message}")


def emit_error(message: str) -> None:
    """Print a red error to stderr (does not exit)."""

    _err_console.print(f"[red]error:[/] {message}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_cell(value: Any) -> str:
    """Render ``value`` as a one-line table cell."""

    if value is None:
        return ""
    if isinstance(value, bool):
        return "✔" if value else "✘"
    if isinstance(value, _dt.datetime):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=_json_default)
    return str(value)


def _json_default(obj: Any) -> Any:
    """``json.dumps`` fallback for datetimes, sets, etc."""

    if isinstance(obj, _dt.datetime):
        return obj.isoformat()
    if isinstance(obj, _dt.date):
        return obj.isoformat()
    if isinstance(obj, set):
        return sorted(obj)
    if hasattr(obj, "model_dump"):  # pydantic v2 models
        return obj.model_dump()
    if hasattr(obj, "dict"):  # pydantic v1 models
        return obj.dict()
    return repr(obj)


@contextmanager
def stderr_console() -> Iterator[Console]:
    """Yield the shared stderr-bound :class:`Console` (used by progress UIs)."""

    yield _err_console


__all__ = [
    "csv_string",
    "emit",
    "emit_csv",
    "emit_error",
    "emit_human",
    "emit_json",
    "emit_kv",
    "emit_table",
    "emit_warn",
    "resolve_mode",
    "stderr_console",
]
