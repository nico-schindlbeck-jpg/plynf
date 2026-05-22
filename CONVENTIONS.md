# Plynf — Development Conventions

> **For implementers**: Read this before opening a PR. These conventions are non-negotiable for v0.1.

## Languages & Versions

- **Python**: 3.11+ (uses PEP 604 union syntax `X | Y`, structural pattern matching ok)
- **TypeScript**: 5.4+, ESM only, target Node 20+
- **Shell**: bash, POSIX-portable where possible

## Style

### Python
- Formatter: `black` (line length 100)
- Linter: `ruff` with rules: E, W, F, I, B, UP, SIM, RET
- Type checker: `mypy --strict` aspirationally; `--ignore-missing-imports` ok in v0.1
- Imports: stdlib → 3rd-party → local, alphabetic within group (ruff handles)
- Docstrings: Google style, every public function/class
- File header: Apache 2.0 SPDX comment

### TypeScript
- Formatter: `prettier` (default config)
- Linter: `eslint` with `@typescript-eslint/recommended`
- Strict mode `tsconfig.json`
- ES modules only

### Tests
- Python: `pytest`, `pytest-asyncio`, `httpx.AsyncClient` for FastAPI
- TypeScript: `vitest`
- Coverage target: ≥ 80% line coverage on `src/` for v0.1

## Naming

- Python packages: `plinth_workspace`, `plinth_gateway`, `plinth` (SDK), `mock_mcp`
- Python modules: snake_case
- TS packages: `@plinth/sdk` (eventually `@plinth/workspace-client` etc.)
- ID prefixes: `ws_`, `kv_`, `file_`, `snap_`, `br_`, `tool_`, `evt_`
- ID format: `<prefix>_<ulid>` (Crockford base32, 26 chars)

## Project Layout

Each Python service follows:
```
services/<name>/
├── pyproject.toml           # uv/pip-installable, declares deps
├── README.md                # what this service is, how to run, how to test
├── Dockerfile               # multi-stage, non-root, distroless or python:3.11-slim
├── src/<package>/
│   ├── __init__.py          # __version__ = "0.1.0"
│   ├── __main__.py          # python -m <package> entrypoint
│   ├── api.py               # FastAPI routes
│   ├── models.py            # Pydantic models (mirror CONTRACTS.md)
│   ├── settings.py          # pydantic-settings, env-driven
│   └── ...
└── tests/
    ├── conftest.py
    ├── test_api.py
    └── ...
```

## Configuration

All services accept config via env vars, prefix `PLINTH_`. Examples:
- `PLINTH_DATA_DIR=/tmp/plinth-data`
- `PLINTH_WORKSPACE_PORT=7421`
- `PLINTH_GATEWAY_PORT=7422`
- `PLINTH_LOG_LEVEL=INFO`
- `PLINTH_LOG_FORMAT=json|console` (default `console` for dev)

## Logging

- Python: `structlog` with key=value or JSON output
- Always include: `service`, `request_id`, `workspace_id` (if applicable), `tool_id` (if applicable)
- Never log secrets, tokens, full file contents

## Error Handling

- Services raise typed exceptions (`WorkspaceNotFound`, `ToolInvocationError`)
- FastAPI exception handlers map to the error model in CONTRACTS.md
- Never swallow exceptions silently. Every `except` either re-raises, logs, or both

## Testing

- Each service has `tests/` mirroring `src/` structure
- Use `httpx.AsyncClient` against the FastAPI app for integration tests
- Use `tmp_path` fixture for filesystem isolation
- Use a fresh SQLite DB per test (in-memory or `tmp_path`)
- Tests must run offline — no real network calls (use `respx` or fixtures)

## Commits & PRs

- Conventional commits: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`
- Each PR has tests
- README quickstart commands MUST work

## License Header (Python)

```python
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
```

(TypeScript files: same as JSDoc comment at top.)

## Forbidden in v0.1

- ❌ Real cloud/OAuth integrations (use mocks)
- ❌ Database migrations as separate tooling (raw SQL on startup is fine)
- ❌ Distributed tracing infra (just structured logs)
- ❌ External secrets managers (env vars only)
- ❌ Multi-region anything

These come post-v0.1 when we have real users.
