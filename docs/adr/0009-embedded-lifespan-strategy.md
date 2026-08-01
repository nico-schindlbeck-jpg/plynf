# ADR 0009: Embedded-Mode Lifespan Strategy

- **Status**: Accepted
- **Date**: 2026-05-15
- **Deciders**: The Plinth Authors
- **Supersedes**: —
- **Related**: ADR 0001 (Language and Stack), upcoming ADR 0010 (Tauri WebView Strategy)

## Context

Block 6 of the Distribution roadmap introduces an Embedded Runtime: five Plinth services (workspace, gateway, identity, dashboard, mock-mcp) running in a single Python process, packaged as one PyInstaller binary, so the Anna persona never has to install Docker.

The natural first attempt is FastAPI's built-in `mount()`:

```python
root = FastAPI()
root.mount("/_workspace", workspace_app)
root.mount("/_gateway", gateway_app)
# ...
```

But Starlette issue [#649](https://github.com/encode/starlette/issues/649) — open since 2019 — documents that **mounted sub-apps do not have their lifespan events fired**. Each Plinth service relies on lifespan startup hooks for: DB connection pool init, AES-GCM key derivation for at-rest encryption, JWT key rotation timer, background OAuth token refresher, and Prometheus metrics registry initialisation. Skipping these silently produces a runtime where reads return 500s for non-obvious reasons.

Block 6 cannot ship until this is resolved. The choice of resolution drives the refactor surface in each of the five services and dictates whether standalone deployments (the existing Compose path that must keep working — see backwards-compatibility contract) are affected.

## Spike

`spike/test_embedded_lifespan.py` benchmarks five candidates against three success criteria:

1. Sub-app lifespan fires before the first request.
2. Standalone-mode `create_app()` keeps working unchanged.
3. Cross-service in-process calls work (Gateway → Identity via `httpx.ASGITransport`).

| Candidate | Lifespan fires? | Standalone unchanged? | Cross-service works? | Verdict |
|---|---|---|---|---|
| A — Raw `mount()` | ❌ | ✅ | ✅ | **Disqualified** — silent failure mode |
| B — AsyncExitStack on root, with `mount()` | ✅ | ✅ | ✅ | Viable |
| C — `include_router()` (services export router instead of app) | ✅ | ⚠️ refactor needed | ✅ | Viable but invasive |
| D — Hybrid: `mount()` + AsyncExitStack dispatching each app's `router.lifespan_context` | ✅ | ✅ | ✅ | **Recommended** |
| E — Cross-service ASGITransport sanity check (uses D as base) | ✅ | ✅ | ✅ | Confirms in-process service-to-service works |

All five tests passed with `asgi-lifespan==2.1.0` driving the lifespan protocol (httpx's `ASGITransport` does NOT send lifespan events by default — this is a separate testing pitfall we documented).

The discriminator between **B** and **D** is bookkeeping: B explicitly composes a fresh `combined_lifespan` per embed, D registers a list of `create_app()` factories and dispatches generically. D adapts more cleanly when a sixth service joins.

The discriminator between **D** and **C**:

- **D** requires zero refactor in existing services. Standalone `uvicorn plinth_workspace.app:app` keeps working unchanged.
- **C** requires every service to export `create_router()` alongside (or instead of) `create_app()`, which is a 5-services-wide change with non-trivial test churn.

## Decision

**Adopt Candidate D — Hybrid mount + AsyncExitStack-dispatched lifespan.**

Concretely, `services/embedded/plinth_embedded/app.py` will look like:

```python
from contextlib import AsyncExitStack, asynccontextmanager
from typing import AsyncIterator, Callable

from fastapi import FastAPI

from plinth_workspace.app import create_app as create_workspace_app
from plinth_gateway.app   import create_app as create_gateway_app
from plinth_identity.app  import create_app as create_identity_app
from plinth_dashboard.app import create_app as create_dashboard_app
from mock_mcp.app         import create_app as create_mock_app


def make_embedded_app() -> FastAPI:
    services: list[tuple[str, FastAPI]] = [
        ("workspace", create_workspace_app(embedded=True)),
        ("gateway",   create_gateway_app(embedded=True)),
        ("identity",  create_identity_app(embedded=True)),
        ("mock",      create_mock_app(embedded=True)),
        ("dashboard", create_dashboard_app(embedded=True)),
    ]

    @asynccontextmanager
    async def composed_lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            for name, sub in services:
                ctx = sub.router.lifespan_context(sub)
                await stack.enter_async_context(ctx)
            yield

    root = FastAPI(title="Plinth Embedded", lifespan=composed_lifespan)
    for name, sub in services[:-1]:
        root.mount(f"/_{name}", sub)
    # Dashboard mounts last at root to serve SPA + assets at "/"
    root.mount("/", services[-1][1])
    return root
```

The `embedded=True` parameter on each `create_app()`:

- Switches DB-URL resolution to `${PLINTH_DATA_DIR}/embedded.db` (single SQLite file with per-service table prefixes, decided in a follow-up ADR if needed; default is one shared file).
- Replaces the HTTP client used for cross-service calls with `httpx.AsyncClient(transport=ASGITransport(app=<sibling_app>))`, wired through a registry that the embedded composer populates after construction.
- Disables OAuth-MCP subprocess spawning (Docker-mode-only feature; UI shows a "Switch to Docker mode" button).

## Consequences

### Positive

- **Zero refactor in existing services for the standalone path.** `uvicorn plinth_workspace.app:create_app --factory` keeps working byte-for-byte. The Compose mode, helm charts, and the existing 1,500+ test suite are unaffected.
- **Lifespan correctness is testable.** The pattern is captured once in `services/embedded/plinth_embedded/app.py`; new services join by adding one line to the `services` list.
- **No new framework primitives.** AsyncExitStack is stdlib (Python 3.7+); `router.lifespan_context` is documented Starlette API since 0.13.
- **Cross-service in-process transport is a small follow-up.** The pattern in test_E generalises: embedded composer constructs an `ASGITransport(app=identity_app)` and injects it into gateway's HTTP-client dependency.

### Negative

- **AsyncExitStack ordering is manual.** If service A's lifespan must complete before service B's starts (e.g., identity must be ready before gateway can fetch keys), the order in the `services` list is load-bearing. We will encode the dependency order as a comment with a justification, and add a property test that swaps the order and asserts startup remains valid (or fails with a clear error).
- **Sub-app middleware does not compose into the root request pipeline.** A middleware registered on `workspace_app` only runs for requests routed to `/_workspace/*`. For middleware that must apply globally (logging, request-id propagation, auth pre-check), we register it on the root app, not on sub-apps. Audit pending of where today's services use middleware.
- **One shared event loop.** All five services share a single asyncio loop. If one service's background task blocks the loop (sync DB call without `to_thread`), it blocks all five. Existing code is already asyncio-correct (uvicorn runs them on one loop in Compose mode too), but the failure mode is now louder.

### Neutral

- `asgi-lifespan==2.1.0` becomes a test-only dependency for embedded tests. Production code does not import it (uvicorn drives lifespan natively at runtime).
- The pattern is also useful for in-process integration tests across services — currently those run as separate processes via testcontainers, which is heavier than it needs to be.

## Alternatives Considered

### B — AsyncExitStack on root, bespoke per embed

Same mechanism as D but the lifespan is hand-written per use case (e.g., per test). Rejected because the embedded composer is the only consumer and centralising the dispatch makes new-service onboarding a one-liner instead of a code-review touchpoint.

### C — APIRouter export

Each service exports `create_router()` and standalone mode does `app = FastAPI(); app.include_router(router)` to maintain the existing surface. Rejected because:
- Five services × ~80 LoC of refactor each = ~400 LoC of moves with corresponding test updates.
- Loses per-service `lifespan=` decorator ergonomics in standalone mode (would have to use `@app.on_event("startup")` shims).
- Doesn't gain anything D doesn't already give us.

### Multi-process via `multiprocessing` in PyInstaller bundle

Spawn five uvicorn processes inside the embedded binary. Rejected because:
- PyInstaller frozen binaries have known headaches with `multiprocessing` (need `freeze_support()`, requires careful entrypoint hygiene per OS, breaks on macOS spawn vs fork).
- Defeats the "single-process embedded" simplicity goal — we'd be re-inventing Compose without the management plane.
- 5× memory overhead vs single-process (each child has its own Python interpreter, ~80 MB).
- Inter-process IPC for cross-service calls (the gateway→identity case) becomes loopback HTTP again, undoing the perf win.

### Refactor Starlette upstream

Fix #649 in Starlette. Rejected because it would tie Plinth's release to upstream merge timing (the issue has been open six years) and any fix would still need a migration path for existing apps.

## Implementation Notes for Block 6

1. Update each of the five service `create_app()` signatures to accept `embedded: bool = False`. Default-False preserves all existing call sites unchanged.
2. Encode service startup order in `services/embedded/plinth_embedded/app.py` as `[identity, workspace, gateway, mock, dashboard]` (identity first so JWT keys are ready when gateway initialises).
3. Add `spike/test_embedded_lifespan.py` as the canonical pattern reference; do not delete it after Block 6 ships — it doubles as living documentation.
4. PyInstaller spec includes `asgi_lifespan` only in test extras, not the runtime bundle.
5. `plinth doctor` (Block 3) gets an `embedded.lifespan_order` check that asserts the documented order matches the registered order.

## Effort Implication

The recommended Candidate D path reduces Block 6 from the v2-plan estimate of **6.5 PT** to **5–6 PT** because no service-side refactor is required. Adjust the Effort Recalibration table accordingly.
