# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the formatters in :mod:`plinth_cli.output`."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from datetime import datetime, timezone

from plinth_cli.output import (
    csv_string,
    emit,
    emit_csv,
    emit_json,
    emit_kv,
    emit_table,
    resolve_mode,
)


def test_resolve_mode_explicit_json() -> None:
    assert resolve_mode("json", "human") == "json"


def test_resolve_mode_explicit_human_when_tty(monkeypatch) -> None:  # noqa: ANN001
    """Non-TTY flips human → json (so pipes stay parseable)."""

    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    assert resolve_mode("human", "json") == "human"


def test_resolve_mode_pipe_falls_back_to_json(monkeypatch) -> None:  # noqa: ANN001
    """Without a TTY, ``human`` defaults flip to ``json``."""

    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    assert resolve_mode(None, "human") == "json"


def test_resolve_mode_unknown_value_defaults_to_human(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    assert resolve_mode("garbage", "garbage") == "human"


def test_emit_json_round_trips_datetime() -> None:
    """``emit_json`` serialises datetimes as ISO-8601 strings."""

    buf = io.StringIO()
    payload = {"when": datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)}
    with redirect_stdout(buf):
        emit_json(payload)
    parsed = json.loads(buf.getvalue())
    assert parsed["when"].startswith("2026-05-08T12:00:00")


def test_emit_table_renders_columns() -> None:
    """``emit_table`` runs without raising and writes something to stdout."""

    buf = io.StringIO()
    with redirect_stdout(buf):
        emit_table("Title", ["A", "B"], [["1", "2"], ["3", "4"]])
    out = buf.getvalue()
    assert "Title" in out


def test_emit_kv_prints_pairs() -> None:
    """``emit_kv`` writes a two-column key/value summary."""

    buf = io.StringIO()
    with redirect_stdout(buf):
        emit_kv("Header", {"x": 1, "y": "two"})
    assert "Header" in buf.getvalue()


def test_resolve_mode_table_aliases_human(monkeypatch) -> None:  # noqa: ANN001
    """``--output table`` and ``--output human`` are equivalent."""

    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    assert resolve_mode("table", "human") == "human"


def test_resolve_mode_csv_passes_through(monkeypatch) -> None:  # noqa: ANN001
    """``csv`` is a first-class output mode."""

    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    assert resolve_mode("csv", "human") == "csv"


def test_emit_csv_writes_header_and_rows() -> None:
    """``emit_csv`` writes RFC-4180 rows starting with the header."""

    buf = io.StringIO()
    with redirect_stdout(buf):
        emit_csv(["a", "b"], [["1", "2"], ["3", "4"]])
    out = buf.getvalue()
    lines = out.splitlines()
    assert lines[0] == "a,b"
    assert lines[1] == "1,2"
    assert lines[2] == "3,4"


def test_csv_string_helper() -> None:
    """``csv_string`` returns the same content but as a string."""

    text = csv_string(["a", "b"], [[1, 2]])
    assert text.startswith("a,b\n")


def test_emit_polymorphic_json() -> None:
    """``emit(rows, headers, mode='json')`` writes JSON."""

    buf = io.StringIO()
    with redirect_stdout(buf):
        emit([{"a": 1, "b": 2}], ["a", "b"], mode="json")
    assert json.loads(buf.getvalue())[0]["a"] == 1


def test_emit_polymorphic_csv() -> None:
    """``emit`` in csv mode produces a header + row."""

    buf = io.StringIO()
    with redirect_stdout(buf):
        emit([{"a": 1, "b": 2}], ["a", "b"], mode="csv")
    out = buf.getvalue()
    assert out.startswith("a,b\n")
    assert "1,2" in out


def test_emit_polymorphic_table() -> None:
    """``emit`` in human/table mode produces the rich table."""

    buf = io.StringIO()
    with redirect_stdout(buf):
        emit([{"a": 1, "b": 2}], ["a", "b"], mode="table", title="T")
    assert "T" in buf.getvalue()
