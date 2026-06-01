# Plynf Dashboard — Personas → Needs → Sections

The dashboard is the central control & monitoring surface. Its information
architecture is derived from four real user types. Each row is a concrete need
and the section that serves it. The dashboard exposes a **role toggle**
(Developer · Lead · Finance · Security) in the top bar that re-weights density
and which "depth" blocks are shown, so the same data serves a CTO and a
finance owner without two products.

## 1. Solo Developer / Indie Hacker
*"Are my agents up, what do they cost today, did anything crash?"*

| Need | Served by |
|------|-----------|
| Instant glance: live status, today's cost, tokens saved | **Overview** — status ampel + savings hero + today's € |
| Minimal friction, no hunting | **Overview** is the landing route; one screen answers "is it fine?" |
| Fast API-key access | **Settings & Safety → Capability Tokens** (copy/rotate inline) |
| "Did something break?" | **Overview → Alerts/DLQ** + **Agents → crash-recovery badges** |

Default role: **Developer** (dense, technical, fast).

## 2. Engineering Lead / Platform Team
*"I own many agents in prod — show me fleet health, audit, and guardrails."*

| Need | Served by |
|------|-----------|
| Multi-agent fleet overview | **Agents & Workflows** — sortable fleet table |
| Workflow status + crash-recovery visibility, resume | **Agents → workflow visualization** with checkpoints + Resume |
| Tool-gateway audit log | **Tool Gateway → Audit Log** |
| DLQ inspection | **Overview → DLQ count → Agents → DLQ drawer** |
| Per-agent rate-limit / cost-cap config | **Settings & Safety → Rate Limits & Cost Caps** |
| Team management | **Settings & Safety → Team & Tenants** |

Role: **Lead** (fleet-wide, operational depth on).

## 3. Finance / Budget Owner (non-technical)
*"One clear number: what does it cost, what do we save?"*

| Need | Served by |
|------|-----------|
| Understandable savings (€ and %) | **Token Economics** — hero € + % with plain-language captions |
| Cost trend over time | **Token Economics → cost-over-time chart** |
| "With vs. without Plynf" comparison | **Token Economics → comparison bars (71–72% reduction)** |
| Exportable reports | **Token Economics → Export (CSV/PDF)** |
| No tech jargon | **Finance role** hides token internals, shows money + sentences |

Role: **Finance** (money-first, jargon hidden, big numbers).

## 4. Security / Compliance Reviewer (Enterprise)
*"I'm here for the security review — prove isolation, audit, and control."*

| Need | Served by |
|------|-----------|
| Full audit trail | **Tool Gateway → Audit Log** + **Settings → Access Log** |
| Multi-tenant isolation | **Settings & Safety → Team & Tenants** (isolation posture) |
| Key-rotation status | **Settings & Safety → Capability Tokens** (rotation age + policy) |
| GDPR export / delete | **Settings & Safety → Data & Compliance** |
| Tamper-evident logs | **Tool Gateway → Audit Log** (hash-chained, verify badge) |
| Access control | **Settings & Safety → Team & Tenants** (roles/RBAC) |

Role: **Security** (compliance panels surfaced, posture front-and-center).

## Section map (routes)

| Route | Section | Primary personas |
|-------|---------|------------------|
| `/app` | Overview / Home | Solo dev, Lead |
| `/app/economics` | Token Economics | Finance, Lead |
| `/app/summaries` | Work Summaries | Solo dev, Finance |
| `/app/agents` | Agents & Workflows | Lead, Solo dev |
| `/app/gateway` | Tool Gateway | Lead, Security |
| `/app/settings` | Settings & Safety | Security, Lead, Solo dev |
| `/app/billing` | Account & Billing | Finance, Solo dev |

## Auth

`/login` on the marketing site offers SSO / OAuth (GitHub, Google, SAML SSO) and
a passwordless magic link — **no plaintext password handling**. On success the
broker redirects to the dashboard origin with the session token in the URL
**fragment** (never a query param, so it stays out of server logs / Referer),
mirroring the existing `apps/oauth-broker` pattern. The dashboard is
domain-ready: the same build serves at `app.plynf.com`.
