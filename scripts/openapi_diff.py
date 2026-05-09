#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Detect breaking OpenAPI changes between two specs.

Usage:
    python3 scripts/openapi_diff.py OLD.yaml NEW.yaml

Exit codes:
    0  no breaking changes (additions are fine)
    1  one or more breaking changes; printed to stderr
    2  invalid input (missing file, malformed YAML, etc.)

Detected breaking changes
-------------------------

- Removed path
- Removed method on a kept path
- Removed response status code on a kept (path, method)
- Removed required request field (request body / parameter)
- Required-ness flip: a request field that was optional is now required
- Type change on an existing field
- Removed enum value
- Removed required response field
- Major version downgrade (e.g. 1.0 -> 0.x)

The script intentionally has zero hard dependencies beyond PyYAML — it's
meant to drop into CI and the Plinth contract test suite without spinning
up an OpenAPI-parsing toolchain. The contract-test runtime in
`tests/contract/src/contract_tests/runner.py` performs a smaller subset of
this comparison against snapshot files; this CLI wraps the same algorithm
plus schema-level checks.

Self-test:
    python3 scripts/openapi_diff.py specs/openapi/workspace.yaml \
                                    specs/openapi/workspace.yaml
    # exits 0 with empty diff
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML missing in user env
    sys.stderr.write(
        "openapi_diff.py requires PyYAML.\n"
        "Install with: pip install pyyaml\n"
    )
    sys.exit(2)


# ---------------------------------------------------------------------------
# Spec loading
# ---------------------------------------------------------------------------


def load_spec(path: Path) -> dict[str, Any]:
    if not path.exists():
        sys.stderr.write(f"error: file not found: {path}\n")
        sys.exit(2)
    try:
        with path.open("r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        sys.stderr.write(f"error: failed to parse {path}: {exc}\n")
        sys.exit(2)
    if not isinstance(doc, dict):
        sys.stderr.write(f"error: {path} did not parse to a YAML mapping\n")
        sys.exit(2)
    return doc


# ---------------------------------------------------------------------------
# Resolution: $ref → component
# ---------------------------------------------------------------------------


def _resolve_ref(doc: dict[str, Any], ref: str) -> dict[str, Any]:
    """Resolve a local `#/...` JSON pointer in `doc`. Returns {} on failure."""
    if not ref.startswith("#/"):
        return {}
    cur: Any = doc
    for chunk in ref[2:].split("/"):
        chunk = chunk.replace("~1", "/").replace("~0", "~")
        if not isinstance(cur, dict) or chunk not in cur:
            return {}
        cur = cur[chunk]
    return cur if isinstance(cur, dict) else {}


def _flatten_schema(doc: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Flatten a schema by following `$ref` once and merging `allOf`.

    Good enough for the contract-stability checks we perform: we only care
    about the top-level `type`, `required`, `properties`, and `enum`, not
    deeply nested polymorphism.
    """
    if not isinstance(schema, dict):
        return {}
    if "$ref" in schema:
        return _flatten_schema(doc, _resolve_ref(doc, schema["$ref"]))
    out: dict[str, Any] = dict(schema)
    if "allOf" in schema and isinstance(schema["allOf"], list):
        merged_props: dict[str, Any] = dict(out.get("properties") or {})
        merged_required: list[str] = list(out.get("required") or [])
        for sub in schema["allOf"]:
            sub_resolved = _flatten_schema(doc, sub)
            for k, v in (sub_resolved.get("properties") or {}).items():
                merged_props.setdefault(k, v)
            for r in sub_resolved.get("required") or []:
                if r not in merged_required:
                    merged_required.append(r)
            for k in ("type", "enum"):
                if k in sub_resolved and k not in out:
                    out[k] = sub_resolved[k]
        if merged_props:
            out["properties"] = merged_props
        if merged_required:
            out["required"] = merged_required
    return out


# ---------------------------------------------------------------------------
# Spec → flat structures
# ---------------------------------------------------------------------------

# An operation key is (path, method) lowercased.
OpKey = tuple[str, str]


@dataclass
class Operation:
    path: str
    method: str
    parameters: dict[str, dict[str, Any]] = field(default_factory=dict)
    request_required: set[str] = field(default_factory=set)
    request_optional: set[str] = field(default_factory=set)
    request_props: dict[str, dict[str, Any]] = field(default_factory=dict)
    responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    response_required: dict[str, set[str]] = field(default_factory=dict)
    response_props: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class FlatSpec:
    title: str = ""
    version: str = ""
    operations: dict[OpKey, Operation] = field(default_factory=dict)
    paths: set[str] = field(default_factory=set)


def flatten(doc: dict[str, Any]) -> FlatSpec:
    info = doc.get("info") or {}
    out = FlatSpec(
        title=str(info.get("title") or ""),
        version=str(info.get("version") or ""),
    )

    for path, ops in (doc.get("paths") or {}).items():
        if not isinstance(ops, dict):
            continue
        out.paths.add(path)
        for method, op in ops.items():
            method_l = method.lower()
            if method_l not in {"get", "post", "put", "patch", "delete", "head", "options"}:
                continue
            if not isinstance(op, dict):
                continue

            operation = Operation(path=path, method=method_l)

            # Parameters: query / path / header / cookie. Required-ness lives
            # on each parameter; we capture it.
            for p in op.get("parameters") or []:
                if not isinstance(p, dict):
                    continue
                if "$ref" in p:
                    p = _resolve_ref(doc, p["$ref"])
                name = p.get("name")
                if not isinstance(name, str):
                    continue
                operation.parameters[name] = {
                    "in": p.get("in"),
                    "required": bool(p.get("required") or False),
                    "schema": p.get("schema") or {},
                }
                if p.get("required"):
                    operation.request_required.add(f"param:{name}")
                else:
                    operation.request_optional.add(f"param:{name}")

            # Request body — focus on application/json schema.
            body = op.get("requestBody") or {}
            if isinstance(body, dict) and "$ref" in body:
                body = _resolve_ref(doc, body["$ref"])
            if isinstance(body, dict):
                content = body.get("content") or {}
                json_part = content.get("application/json") or {}
                schema = _flatten_schema(doc, json_part.get("schema") or {})
                req = set(schema.get("required") or [])
                props = schema.get("properties") or {}
                if isinstance(props, dict):
                    for prop_name, prop_schema in props.items():
                        operation.request_props[prop_name] = (
                            _flatten_schema(doc, prop_schema)
                            if isinstance(prop_schema, dict) else {}
                        )
                        if prop_name in req:
                            operation.request_required.add(f"body:{prop_name}")
                        else:
                            operation.request_optional.add(f"body:{prop_name}")

            # Responses — capture status codes + response body schema.
            for code, resp in (op.get("responses") or {}).items():
                code_s = str(code)
                if isinstance(resp, dict) and "$ref" in resp:
                    resp = _resolve_ref(doc, resp["$ref"])
                operation.responses[code_s] = resp if isinstance(resp, dict) else {}
                if not isinstance(resp, dict):
                    continue
                content = resp.get("content") or {}
                json_part = content.get("application/json") or {}
                schema = _flatten_schema(doc, json_part.get("schema") or {})
                operation.response_required[code_s] = set(schema.get("required") or [])
                props = schema.get("properties") or {}
                if isinstance(props, dict):
                    for prop_name, prop_schema in props.items():
                        operation.response_props[f"{code_s}:{prop_name}"] = (
                            _flatten_schema(doc, prop_schema)
                            if isinstance(prop_schema, dict) else {}
                        )

            out.operations[(path, method_l)] = operation

    return out


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


@dataclass
class BreakingChange:
    kind: str
    where: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.kind}] {self.where} — {self.detail}"


def _scalar_type(schema: dict[str, Any]) -> str:
    if not isinstance(schema, dict):
        return ""
    t = schema.get("type")
    if isinstance(t, list):
        return "|".join(sorted(str(x) for x in t))
    if isinstance(t, str):
        return t
    return ""


def _enum_set(schema: dict[str, Any]) -> set[str]:
    if not isinstance(schema, dict):
        return set()
    e = schema.get("enum")
    if not isinstance(e, list):
        return set()
    return {str(v) for v in e}


def _major(version: str) -> int:
    if not version:
        return 0
    head = version.split(".", 1)[0]
    try:
        return int(head)
    except ValueError:
        return 0


def diff(old: FlatSpec, new: FlatSpec) -> list[BreakingChange]:
    out: list[BreakingChange] = []

    # Major version downgrade.
    old_major = _major(old.version)
    new_major = _major(new.version)
    if old_major and new_major and new_major < old_major:
        out.append(
            BreakingChange(
                "version-downgrade",
                "info.version",
                f"{old.version} -> {new.version}",
            )
        )

    # Removed paths.
    for p in sorted(old.paths - new.paths):
        out.append(BreakingChange("removed-path", p, "path no longer documented"))

    # Removed methods on kept paths.
    for path in sorted(old.paths & new.paths):
        old_methods = {m for (p, m) in old.operations if p == path}
        new_methods = {m for (p, m) in new.operations if p == path}
        for m in sorted(old_methods - new_methods):
            out.append(BreakingChange("removed-method", f"{m.upper()} {path}", "method removed"))

    # Per-operation diffs.
    for key in sorted(old.operations.keys() & new.operations.keys()):
        old_op = old.operations[key]
        new_op = new.operations[key]
        op_label = f"{key[1].upper()} {key[0]}"

        # Removed status codes.
        for status in sorted(set(old_op.responses) - set(new_op.responses)):
            out.append(
                BreakingChange("removed-status", op_label, f"response {status} removed")
            )

        # Removed required request fields.
        for f_ in sorted(old_op.request_required - new_op.request_required - new_op.request_optional):
            out.append(
                BreakingChange("removed-required-field", op_label, f"{f_}")
            )

        # Optional -> required flip on a request field.
        flip = old_op.request_optional & new_op.request_required
        for f_ in sorted(flip):
            out.append(
                BreakingChange("required-flip", op_label, f"{f_} optional -> required")
            )

        # Type change on a kept request property.
        common_props = set(old_op.request_props) & set(new_op.request_props)
        for prop in sorted(common_props):
            old_t = _scalar_type(old_op.request_props[prop])
            new_t = _scalar_type(new_op.request_props[prop])
            if old_t and new_t and old_t != new_t:
                out.append(
                    BreakingChange(
                        "type-change",
                        f"{op_label} body:{prop}",
                        f"{old_t} -> {new_t}",
                    )
                )
            old_enum = _enum_set(old_op.request_props[prop])
            new_enum = _enum_set(new_op.request_props[prop])
            removed_enum = old_enum - new_enum
            if old_enum and removed_enum:
                out.append(
                    BreakingChange(
                        "removed-enum-value",
                        f"{op_label} body:{prop}",
                        f"removed: {sorted(removed_enum)}",
                    )
                )

        # Added required field on a kept request body — breaking for clients.
        added_required = new_op.request_required - old_op.request_required - old_op.request_optional
        # Filter out additions that didn't exist in old (they're net-new
        # endpoints); we only flag fields that existed and got demanded.
        # In practice "didn't exist before" is fine: it's only breaking when
        # the surface as a whole already existed.
        for f_ in sorted(added_required):
            out.append(
                BreakingChange("added-required-field", op_label, f"{f_}")
            )

        # Removed required response field.
        for status in sorted(set(old_op.response_required) & set(new_op.response_required)):
            removed = old_op.response_required[status] - (
                new_op.response_required.get(status) or set()
            )
            for f_ in sorted(removed):
                out.append(
                    BreakingChange(
                        "removed-required-response-field",
                        f"{op_label} {status}",
                        f_,
                    )
                )

        # Type change on a kept response property.
        common_resp = set(old_op.response_props) & set(new_op.response_props)
        for key_resp in sorted(common_resp):
            old_t = _scalar_type(old_op.response_props[key_resp])
            new_t = _scalar_type(new_op.response_props[key_resp])
            if old_t and new_t and old_t != new_t:
                out.append(
                    BreakingChange(
                        "type-change",
                        f"{op_label} response {key_resp}",
                        f"{old_t} -> {new_t}",
                    )
                )

    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Detect breaking OpenAPI changes between two specs.",
    )
    parser.add_argument("old", type=Path, help="Path to the OLD spec (e.g. base branch).")
    parser.add_argument("new", type=Path, help="Path to the NEW spec (e.g. PR branch).")
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the 'no breaking changes' line on success.",
    )
    args = parser.parse_args(argv)

    old_doc = load_spec(args.old)
    new_doc = load_spec(args.new)

    old_flat = flatten(old_doc)
    new_flat = flatten(new_doc)

    changes = diff(old_flat, new_flat)
    if not changes:
        if not args.quiet:
            sys.stdout.write(
                f"openapi-diff: no breaking changes "
                f"({args.old.name} -> {args.new.name})\n"
            )
        return 0

    sys.stderr.write(
        f"openapi-diff: {len(changes)} breaking change(s) detected "
        f"({args.old.name} -> {args.new.name}):\n"
    )
    for change in changes:
        sys.stderr.write(f"  {change}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
