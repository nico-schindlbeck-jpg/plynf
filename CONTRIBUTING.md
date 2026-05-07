# Contributing to Plinth

Thanks for your interest! Plinth is in early PoC stage — the best contributions right now sharpen the primitives, the docs, and the demos.

## Getting set up

```bash
git clone https://github.com/your-org/plinth.git
cd plinth
make install      # creates .venv, installs all packages in editable mode
make test         # runs all tests
make demo         # runs the headline token-comparison demo
```

Requirements: Python 3.11+, Node 20+ (only if working on the TS SDK), make.

## How to propose a change

1. **Open an issue first** for non-trivial changes. Sketch the problem and proposed approach.
2. **Read [CONTRACTS.md](./CONTRACTS.md) and [CONVENTIONS.md](./CONVENTIONS.md)** before writing code.
3. **Branch naming**: `feat/...`, `fix/...`, `docs/...`, `refactor/...`.
4. **Commits**: conventional-commits style (`feat: add branch merge endpoint`).
5. **Tests**: every PR with code changes adds or updates tests.
6. **Docs**: if you change behaviour, update the relevant `docs/` and `CONTRACTS.md`.
7. **PR**: link the issue, describe the change, list manual testing steps.

## Good first PRs

- Add a new tool to `mock-mcp-server/` (e.g., a calculator, a calendar, a mock GitHub)
- Add an example agent in `examples/` that exercises a Plinth feature
- Improve an existing ADR or write a new one for a design question
- Add a missing test (especially edge cases for snapshots / branches)
- Improve the TypeScript SDK toward parity with Python

## What we're *not* taking yet

- Production observability backends (Prometheus exporters etc.) — comes later
- Real OAuth integrations — design first via ADR
- Major architectural changes without an ADR
- Code without tests

## Code review

Expect 1–3 review rounds. We push back on:
- Skipped tests
- Inconsistency with `CONTRACTS.md`
- Tight coupling between services
- Hidden side effects
- Logging secrets

## Discussion

Issues are the canonical channel for design discussion. We don't have Slack / Discord yet — that comes if there's enough community traction.

## Code of Conduct

By participating you agree to the [Contributor Covenant Code of Conduct](https://www.contributor-covenant.org/version/2/1/code_of_conduct/).

## License

By contributing you agree that your contributions are licensed under Apache 2.0 (see [LICENSE](./LICENSE)).
