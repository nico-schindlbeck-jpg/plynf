# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Helpers shared between the workspace storage drivers."""

from __future__ import annotations


def translate_placeholders_to_postgres(sql: str) -> str:
    """Convert SQLite ``?`` placeholders to Postgres ``$1, $2, ...``.

    The implementation walks character-by-character so we don't transform a
    literal ``?`` inside a quoted string. ``?`` is rare in JSON values, but
    correctness is cheap so we keep the guard.

    Examples::

        >>> translate_placeholders_to_postgres("SELECT 1 WHERE a=?")
        'SELECT 1 WHERE a=$1'
        >>> translate_placeholders_to_postgres("INSERT INTO t VALUES (?, ?)")
        'INSERT INTO t VALUES ($1, $2)'
        >>> translate_placeholders_to_postgres("SELECT '?', ? FROM t")
        "SELECT '?', $1 FROM t"
    """

    out: list[str] = []
    i = 0
    n = len(sql)
    counter = 0
    in_single = False
    in_double = False
    while i < n:
        ch = sql[i]
        # Track single-quoted string literals (and escape ``''``).
        if ch == "'" and not in_double:
            out.append(ch)
            if in_single and i + 1 < n and sql[i + 1] == "'":
                out.append("'")
                i += 2
                continue
            in_single = not in_single
            i += 1
            continue
        if ch == '"' and not in_single:
            out.append(ch)
            in_double = not in_double
            i += 1
            continue
        if ch == "?" and not in_single and not in_double:
            counter += 1
            out.append(f"${counter}")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


__all__ = ["translate_placeholders_to_postgres"]
