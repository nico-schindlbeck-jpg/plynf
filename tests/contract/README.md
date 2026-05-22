# Plynf — Contract tests

Verifies that the running services match the OpenAPI specs in
`specs/openapi/` and flags breaking changes between snapshots.

## Run

```bash
cd tests/contract
pip install -e ".[dev]"
pytest -q
```

## What's covered

- Every documented `/v1/` path exists in the running FastAPI app.
- Every method documented for a path is wired up.
- Every response status code documented for a `(path, method)` exists in the
  app's OpenAPI output (we only enforce `present`, not exact schema match —
  schema-level checks would be too brittle pre-frozen-spec).
- A baseline snapshot in `tests/snapshots/` is compared against the live
  spec; removed paths / removed required fields / changed response shapes
  are flagged as breaking.

Tests where a service is not importable on the current Python path skip
gracefully so the suite runs in any worktree.

## Design

- `src/contract_tests/runner.py` defines `ContractCheck` — a small dataclass
  describing a single check's input + verdict, plus helpers that load
  OpenAPI documents from FastAPI apps and from on-disk YAML.
- `src/contract_tests/<service>.py` builds the FastAPI app for that service
  using only test-safe defaults and exposes `load_actual_spec()` /
  `load_expected_spec()`.
- `tests/test_<service>_contract.py` invokes the helpers and asserts.
- `tests/test_breaking_changes.py` diffs the live spec against
  `tests/snapshots/<service>.yaml`.

The suite is designed to run **without** a live network — every check
operates on `app.openapi()` from an in-process FastAPI app.
