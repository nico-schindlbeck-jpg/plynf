"""Plynf Embedded — all five core services in one Python process.

Single-binary alternative to the 13-container Compose stack. Bundles
workspace, gateway, identity, dashboard, and mock-mcp into one uvicorn
process listening on a single port. The OAuth-MCP servers are
intentionally NOT included — they need Docker to spawn per-provider
subprocesses, and bundling all 8 would balloon the binary past 600 MB.

Architecture pattern: AsyncExitStack composition over service factories.
Each service exports a `create_app(embedded: bool)` factory. The embedded
composer mounts them all under one root FastAPI app, then dispatches each
service's lifespan via a stack so DB pools, JWT keys, and background
tasks initialize correctly. See docs/adr/0009-embedded-lifespan-strategy.md
for the alternatives we ruled out.
"""

__version__ = "0.1.0"

from plynf_embedded.app import make_embedded_app

__all__ = ["make_embedded_app"]
