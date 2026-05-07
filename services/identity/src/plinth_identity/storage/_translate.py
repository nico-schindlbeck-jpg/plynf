# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""SQL placeholder translation helper."""

from __future__ import annotations


def translate_placeholders_to_postgres(sql: str) -> str:
    """Convert SQLite ``?`` placeholders to Postgres ``$1, $2, ...``.

    Walks character-by-character so a literal ``?`` inside a quoted string
    is left alone.
    """

    out: list[str] = []
    i = 0
    n = len(sql)
    counter = 0
    in_single = False
    in_double = False
    while i < n:
        ch = sql[i]
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
