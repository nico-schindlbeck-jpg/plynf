# Security Policy

## Status

Plinth is currently a **proof-of-concept (v0.1)**. It is **NOT production-ready** and should not be used in production environments without significant hardening.

## Known limitations in v0.1

- Authentication is a static bearer token (any non-empty string accepted)
- No multi-tenancy isolation
- SQLite is not encrypted at rest
- No rate limiting
- No audit log integrity (no cryptographic chaining)
- OAuth credentials in the gateway are stored as JSON in the gateway DB (acceptable for local dev only)

These will be addressed before any v1.0 / production release.

## Reporting a vulnerability

For real security issues, please **do not** open a public GitHub issue. Instead:

- Email: security@plinth.example (placeholder for the PoC repo)
- Include: a description, reproduction steps, and your assessment of severity
- We aim to respond within 7 days

## Scope

In scope:
- Workspace service (`services/workspace/`)
- Tool gateway (`services/gateway/`)
- SDKs (`sdk/python/`, `sdk/typescript/`)

Out of scope (not security-sensitive in PoC):
- Mock MCP server (intentionally permissive for demo)
- Example agents

## Disclosure

We follow coordinated disclosure: please give us reasonable time to fix issues before public disclosure.
