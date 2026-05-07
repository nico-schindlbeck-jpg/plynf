# Triage report — demo/repo

_Mode_: `simulation`
_Total issues triaged_: 10

## Counts
- **bug**: 3
- **feature**: 3
- **question**: 3
- **spam**: 1

## Per-issue classifications
### #101 — Crash on startup when config is missing
- _category_: **bug**
- _confidence_: 0.80
- _rationale_: title/body indicates a defect
- _author_: alice
- _url_: <https://github.com/demo/repo/issues/101>

> The CLI fails with a stack trace if I run it without a config file. Expected behaviour is to print a friendly error.

### #102 — Add support for YAML config
- _category_: **feature**
- _confidence_: 0.75
- _rationale_: reads like a feature request
- _author_: bob
- _url_: <https://github.com/demo/repo/issues/102>

> It would be nice to add YAML config support next to TOML.

### #103 — How do I configure the cache TTL?
- _category_: **question**
- _confidence_: 0.65
- _rationale_: phrased as a question
- _author_: carol
- _url_: <https://github.com/demo/repo/issues/103>

> I can't find documentation on how to set a custom cache TTL.

### #104 — Buy our SEO services 🎉🎉🎉 click here
- _category_: **spam**
- _confidence_: 0.95
- _rationale_: matched spam keywords
- _author_: spammer42
- _url_: <https://github.com/demo/repo/issues/104>

> 100% free promotional offer just for you!

### #105 — Regression: list endpoint returns 500 on empty repos
- _category_: **bug**
- _confidence_: 0.80
- _rationale_: title/body indicates a defect
- _author_: dave
- _url_: <https://github.com/demo/repo/issues/105>
- _existing labels_: needs-triage

> After upgrading to v0.3 the list endpoint throws an exception when the repo is empty. Was working in v0.2.

### #106 — Feature request: add CSV export
- _category_: **feature**
- _confidence_: 0.75
- _rationale_: reads like a feature request
- _author_: erin
- _url_: <https://github.com/demo/repo/issues/106>

> Could the workspace files API expose a CSV export option?

### #107 — Is there a way to filter audit events by tenant?
- _category_: **question**
- _confidence_: 0.65
- _rationale_: phrased as a question
- _author_: frank
- _url_: <https://github.com/demo/repo/issues/107>

> I'd like to see audit events for one tenant at a time.

### #108 — Incorrect cost estimate for cached calls
- _category_: **bug**
- _confidence_: 0.80
- _rationale_: title/body indicates a defect
- _author_: grace
- _url_: <https://github.com/demo/repo/issues/108>
- _existing labels_: bug

> The dashboard shows non-zero cost for tool calls served from the cache. Should be zero.

### #109 — Proposal: snapshot retention policy
- _category_: **feature**
- _confidence_: 0.75
- _rationale_: reads like a feature request
- _author_: harry
- _url_: <https://github.com/demo/repo/issues/109>

> Add an option to auto-prune old snapshots after N days.

### #110 — Question on workflow resume semantics
- _category_: **question**
- _confidence_: 0.65
- _rationale_: phrased as a question
- _author_: ivy
- _url_: <https://github.com/demo/repo/issues/110>

> Does workflow resume restore the workspace KV at the snapshot point?
