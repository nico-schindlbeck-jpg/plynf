# SDK Publishing — Operator Guide

How to ship a new SDK version across all five languages. Trigger-based, tag-driven, no API tokens in CI.

## Status

| SDK | Workflow | Registry | First publish |
|---|---|---|---|
| Python | `release-python-sdk.yml` | PyPI · trusted publishing | ⬜ Pending org setup |
| TypeScript | `release-ts-sdk.yml` | npm · trusted publishing + provenance | ⬜ Pending @plinth org claim |
| Go | `release-go-sdk.yml` | proxy.golang.org (pull-based) | ⬜ Pending go.mod path decision |
| Swift | manual one-time | Swift Package Index | ⬜ Pending repo registration |
| Kotlin | manual gradle | Maven Central (OSSRH) | ⏸ Deferred to v1.7 |

## Tag conventions

| What you release | Tag format | Workflow trigger |
|---|---|---|
| Plinth runtime | `v1.7.5` | release-images.yml (Block 2) |
| Python SDK | `sdk-python-v0.2.0` | release-python-sdk.yml |
| TypeScript SDK | `sdk-ts-v0.4.0` | release-ts-sdk.yml |
| Go SDK | `sdk-go-v0.4.0` | release-go-sdk.yml |
| Swift SDK | `sdk-swift-v0.2.0` | Swift Package Index auto-detects |
| Kotlin SDK | `sdk-kotlin-v0.2.0` | (manual gradle until v1.7) |

Rule: an SDK tag is **independent** from the runtime version. The runtime contract is API v1; SDK versions can drift independently. Major bumps require a v2/ namespace coordination, documented per the API v1 contract.

## Pre-flight (one-time per language)

### Python · PyPI

1. **Claim the package name on PyPI.** Visit https://pypi.org/project/plinth/ — if 404, run one manual publish from a local dev box to reserve:
   ```bash
   cd sdk/python
   python -m build
   twine upload dist/*   # one-time only, with a maintainer's API token
   ```
2. **Register Trusted Publisher** at https://pypi.org/manage/account/publishing/:
   - Owner: `nico-schindlbeck-jpg` (or new org)
   - Repository: `plinth`
   - Workflow filename: `release-python-sdk.yml`
   - Environment name: `pypi`
3. From this point on, all releases happen via tag push.

### TypeScript · npm

1. **Claim `@plinth` org** at https://www.npmjs.com/org/create (or fall back to package name `plinth-sdk` if `@plinth` taken).
2. **First publish manually** to seed the package:
   ```bash
   cd sdk/typescript
   npm publish --access public
   ```
3. **Enable Trusted Publisher** at https://www.npmjs.com/package/@plinth/sdk/access:
   - Organization: `nico-schindlbeck-jpg`
   - Repository: `plinth`
   - Workflow: `release-ts-sdk.yml`
   - Environment: `npm`

### Go · proxy.golang.org

**Pre-flight decision required** (blocks first Go release):

- **Option A — Move to dedicated repo `github.com/plinth/sdk-go`.** Pro: clean module path matching go.mod. Con: contributors must sync two repos.
- **Option B — Keep in monorepo, change `go.mod` to `github.com/nico-schindlbeck-jpg/plinth/sdk/go`.** Pro: single repo. Con: ugly import path, doesn't match Plinth branding.

Recommendation: **Option A.** A dedicated read-only mirror, auto-synced from this monorepo on tag push, is the standard pattern for monorepo'd Go modules.

Once decided:
1. Create the target repo (if Option A)
2. Push first tag: `sdk-go-v0.1.0`
3. Workflow validates build, pings `proxy.golang.org` for cache-warm
4. Consumers can `go get github.com/plinth/sdk-go@v0.1.0`

### Swift · Swift Package Index

One-time:
1. Submit repo at https://swiftpackageindex.com/add-a-package
2. URL: `https://github.com/nico-schindlbeck-jpg/plinth.git` (or dedicated mirror)
3. Wait ~10 min for index pickup
4. Future `Package.swift` consumers add `.package(url: "https://github.com/.../plinth.git", from: "0.2.0")`

There is no recurring release workflow — Swift PI re-indexes on every push to main and on every tag automatically.

### Kotlin · Maven Central (deferred)

Maven Central via Sonatype OSSRH is the most complex pipeline:
- Requires Sonatype account approval (1-2 weeks lead time)
- GPG signing setup
- `groupId` namespace verification (we'd need `dev.plinth` claim, requires DNS TXT record at plinth.dev)

Deferred to v1.7. Until then, Kotlin SDK consumers add a Maven `repositories { ... }` block pointing at a self-hosted artefact server OR vendor the SDK as a sub-module.

## Day-to-day release procedure

### Python

```bash
# 1. Bump version
$EDITOR sdk/python/pyproject.toml          # version = "0.2.0"
# 2. PR
git checkout -b sdk/python/v0.2.0
git commit -am "sdk(python): v0.2.0"
gh pr create --fill && gh pr merge --squash
# 3. Tag from main
git checkout main && git pull
git tag sdk-python-v0.2.0
git push origin sdk-python-v0.2.0
# 4. Watch the workflow at .github/workflows/release-python-sdk.yml
gh run watch
```

### TypeScript

```bash
$EDITOR sdk/typescript/package.json        # "version": "0.4.0"
# ... same flow with tag sdk-ts-v0.4.0
```

### Go

```bash
$EDITOR sdk/go/plinth/version.go           # const Version = "0.4.0"
# ... same flow with tag sdk-go-v0.4.0
```

## Verification after release

Each workflow leaves a step summary on the run page with consumer install commands. Smoke-test in a scratch dir:

```bash
# Python
python -m venv /tmp/sdk-test && /tmp/sdk-test/bin/pip install plinth==0.2.0
/tmp/sdk-test/bin/python -c "from plinth import Plinth; print(Plinth)"

# TypeScript
mkdir /tmp/sdk-test-ts && cd /tmp/sdk-test-ts && npm init -y
npm install @plinth/sdk@0.4.0
node -e "console.log(require('@plinth/sdk'))"

# Go
mkdir /tmp/sdk-test-go && cd /tmp/sdk-test-go
go mod init test && go get github.com/plinth/sdk-go@v0.4.0
echo 'package main; import "github.com/plinth/sdk-go/plinth"; func main(){ _ = plinth.Version }' > main.go
go run main.go
```

If any of these fail, the workflow's "Publish" step rolled forward but verification didn't — open an issue and tag the relevant SDK maintainer.

## Failure modes + recovery

| Symptom | Likely cause | Recovery |
|---|---|---|
| `403 Forbidden` from PyPI | Trusted Publisher not configured | Re-register at pypi.org/manage/account/publishing |
| npm `403` with `must be authenticated` | `@plinth` org membership missing | Add bot account, or fall back to manual `NPM_TOKEN` (avoid) |
| `go get` returns `unknown revision` | Tag not on default branch, or proxy cache cold | Wait 1 hour, OR run `GOPROXY=direct go get` to bypass cache |
| Workflow tag mismatch error | `sdk-X-vY.Z` tag doesn't match version in source | Delete the bad tag, fix the version, re-tag |
| `pyproject.toml` version forgotten | Sanity check catches before publish | Bump + commit + re-tag |

## When to deprecate a version

If a published version has a critical bug:

1. **Python**: `pip yank` (preferred) — `twine upload --yank "0.2.0" --reason "..."`. Yanked versions are not installed by default but can be force-installed.
2. **npm**: `npm deprecate @plinth/sdk@"0.4.0" "Critical bug in X, use 0.4.1+"`.
3. **Go**: cannot delete. Tag a `0.4.1` with the fix; document the bad version in CHANGELOG.

Never delete a tag from git. Once a SDK version is published, consumers may already depend on it.

## CI ownership

These workflows are owned by the Distribution workstream. Day-to-day failures should go to the maintainer of the affected SDK (see `CODEOWNERS`). Cross-cutting changes (e.g., switching to a different signing approach) get an ADR.
