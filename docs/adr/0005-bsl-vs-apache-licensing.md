# ADR 0005: Licensing — Apache 2.0 for v0.1, BSL for v1.0 Production Runtime

- **Status**: Accepted
- **Date**: 2026-05-05
- **Deciders**: The Plinth Authors

## Context

Plinth is open-source by design. The choice of *which* open-source license is a strategic decision with significant downstream consequences for adoption, community, monetisation, and competitive defensibility against hyperscalers.

The recurring pattern in our space:

- Open-core companies (MongoDB, Elastic, Confluent, HashiCorp, Redis Inc.) start under a permissive license (Apache, MIT) and watch hyperscalers (AWS, GCP, Azure) capture most of the commercial value by offering managed versions of their software without commensurate contribution.
- These companies then adopt source-available licenses (BSL, SSPL, ELv2) for their core products to prevent commercial-as-a-service competition.
- The fallout is real: community pushback, forks (OpenSearch, Valkey, Garnet), regulatory scrutiny in some markets.

We have to pick our license posture *before* we have a hyperscaler problem, because changing later is the painful part.

The constraints in our case:

- **SDK adoption depends on permissive licensing.** Engineers will refuse a non-permissive SDK on the grounds that it might infect their own code's license. Adoption blockers here kill us.
- **Self-hosters are the right default audience for v0.1.** People should be able to clone the repo, run Plinth on their infra, modify it, and ship products on top — without lawyers in the loop.
- **A future managed offering is in our roadmap.** Plinth as substrate-as-a-service is a plausible business model. We should not paint ourselves into a corner where AWS can ship "Amazon Plinth" and capture the commercial value while we maintain the code.
- **Customer trust requires source availability.** Enterprise buyers want the source even if they don't intend to fork. They want to audit it, fork it if we go away, and modify it if needed. A closed-source v1.0 is not credible.

The licensing options that fit:

- **Apache 2.0.** Permissive, OSI-approved, no anti-SaaS terms.
- **MIT.** Same as Apache 2.0 minus the patent grant. Apache is strictly better for a commercial-relevant project.
- **BSL (Business Source License) 1.1.** Source-available, time-limited usage restriction (the "Additional Use Grant" defines what you may do; everything else requires a commercial agreement). After a defined period (typically 4 years), the code converts to a permissive license you nominate. Used by MariaDB, CockroachDB, Sentry.
- **SSPL.** A copyleft license requiring service operators to open-source their entire stack. Aggressive; OSI-rejected. Used by MongoDB.
- **ELv2 (Elastic License v2).** Source-available with three explicit prohibitions (no SaaS hosting, no removing licensing, no circumventing). Lighter than SSPL.

## Decision

**Apache 2.0 for v0.1 across the entire repository (services, SDKs, examples, mock-mcp). The v1.0 production runtime moves to BSL 1.1 with a 4-year transition to Apache 2.0. SDKs remain Apache 2.0 forever.**

Specifically:

- v0.1 (the PoC, this codebase as it stands): **all Apache 2.0**, declared at the repo root and per-package. This is the license under which the docs you are reading were written.
- The split at v1.0:
  - `sdk/python`, `sdk/typescript`: **stay Apache 2.0 forever.** SDKs are user-facing code; permissive licensing here is non-negotiable for adoption.
  - `services/workspace`, `services/gateway`, `services/identity`, `services/workflow` (the production-runtime services): **become BSL 1.1** with an Additional Use Grant permitting non-commercial use, internal use, and any use that does not constitute "offering Plinth as a managed service". Each release converts to Apache 2.0 four years after that release.
  - `mock-mcp-server`, `examples/`, `docs/`: **stay Apache 2.0 forever.** These are reference material.
- Specs (`specs/openapi`, `specs/proto`, `specs/schemas`): **stay Apache 2.0 forever**, plus an explicit grant that they may be implemented by anyone in any license. The protocol must be free; the implementation can be BSL.
- Existing v0.1 contributions remain Apache 2.0 in perpetuity. The BSL change applies forward, not retroactively. We document a clear release note when the change happens.

Contributor License Agreement: we'll require a lightweight CLA from contributors to the production-runtime services so that the relicense path is legally clean. The CLA will state explicitly that contributions can be relicensed under any OSI-approved or source-available license. SDK contributions need only the standard Apache 2.0 contribution agreement (no CLA).

## Consequences

### Positive

- **No adoption friction at v0.1.** Today's audience is engineers building agent prototypes. Apache everything means zero legal questions.
- **Permissive SDKs forever.** The most-adopted surface of Plinth (the SDKs, especially Python and TS) will never have a license question. This is critical for embedding in customer codebases.
- **BSL preserves the option of a commercial managed service.** When/if we offer hosted Plinth, hyperscalers can't trivially copy our service. This is the same playbook MariaDB, CockroachDB, and Sentry use, with documented success.
- **4-year Apache transition is honest.** Old releases become permissive after 4 years. We're not pretending to be open-source forever while keeping a hostage; we're saying "we get 4 years of head start on each release, then it's free for anyone".
- **Spec independence.** Anyone can build a Plinth-compatible service. The protocol is permissive even when the implementation isn't. This matters for ecosystem trust.

### Negative / Trade-offs

- **Transition cost.** The move from Apache to BSL at v1.0 is a friction event. We will get pushback. We should over-communicate the rationale (this ADR exists in part for that reason) and make sure the CLA path doesn't surprise contributors.
- **BSL is not OSI-approved.** Some buyers have policies that reject "non-OSI-approved" licenses. We accept this; the alternative (Apache forever for the production runtime) means tolerating hyperscaler appropriation. We can grant individual customers under custom commercial terms when policy is the blocker.
- **Two-license repository complexity.** Maintaining "this directory is BSL, this is Apache" in a single repo is operationally fiddly. We will use SPDX headers per file and a top-level NOTICE that maps directories to licenses. CI checks reject mismatched headers.
- **Contributor friction post-v1.0.** A CLA for production-runtime contributions is unusual in the agent-tooling ecosystem. Some developers won't sign a CLA on principle. We accept that some contributors will only contribute to SDKs (which is fine — that's where most contributions go anyway in our space).
- **Forks are still possible.** A community fork at the last Apache-licensed v1.0-pre release is a real risk. We mitigate by making the BSL terms reasonable (the Additional Use Grant is generous, only the SaaS-of-Plinth case is restricted).
- **Reputational risk in the OSS community.** Plinth would publicly join the "company that relicensed" club. We accept this; the precedents (MariaDB, CockroachDB, Sentry) survived and remain credible. Operations like Elastic's relicensing show how to do it badly; we'll study and not repeat.

## Alternatives Considered

### Apache 2.0 for everything, forever

The most permissive, most adoption-friendly option. Why we don't pick it for v1.0:

- **AWS-rebrands-Plinth scenario.** A hyperscaler can wrap our services, brand them, and sell them. We get nothing for a software stack we maintain.
- The companies that have stayed Apache (Kubernetes, Postgres, Linux) are all backed by foundations or have multiple commercial sponsors. We are a small startup; the same posture isn't tenable for us.

### MIT for everything

Strictly worse than Apache 2.0 for our use case (no patent grant). Rejected.

### SSPL for the production runtime

Aggressive: forces SaaS operators to open-source their entire stack. Used by MongoDB. Why we don't pick it:

- Not OSI-approved, and the rationale for that rejection (overreach into adjacent code) is something we don't want to inherit.
- The community pushback to MongoDB's relicense was significant. We can get most of the commercial protection from BSL with much less reputational damage.

### ELv2 for the production runtime

Closer to BSL than SSPL: a source-available license with three specific prohibitions. Used by Elastic post-2021. The arguments against:

- ELv2 is not OSI-approved either, and is more restrictive than BSL in some ways (the "no provide as managed service" clause is permanent, not time-limited).
- BSL's 4-year-to-Apache transition is more honest about our intent: "we want 4 years of head start, then it's truly free".

We took ELv2 seriously; BSL won on the time-limit grace.

### Source-available "you may use, you may not redistribute"

Rejected. Customers want to fork in case we go away. Removing redistribution is incompatible with that, and it kills the "you can audit us" pitch.

### Closed-source production runtime, open SDKs

Considered briefly. Rejected because enterprise buyers want the source for self-host and for trust reasons. Closed source for the runtime would block our enterprise motion entirely.

### Dual licensing (Apache + commercial)

Some projects publish under Apache and offer commercial licenses for specific features. Considered. The challenge: most commercially-relevant features in Plinth (multi-tenancy, identity, observability) are platform-wide concerns, not optional add-ons we can license separately. The dual-license model works better for things like database-engine plugins. Not a fit.

## Notes / Links

- BSL 1.1 specification: external — `https://mariadb.com/bsl11/`
- Comparable relicenses for context (study but don't copy verbatim):
  - MariaDB: BSL with auto-conversion to GPL after 4 years (the precedent we're aligned with).
  - CockroachDB: BSL with auto-conversion to Apache 2.0.
  - Elastic: ELv2 (different model; more restrictive).
  - Sentry: FSL with 2-year Apache conversion (similar shape, faster transition).
- This ADR pairs with the README's licensing note and (post-v1.0) a `LICENSING.md` that maps directories to licenses with SPDX identifiers.
- CLA tool when relevant: `EasyCLA` (Linux Foundation) or `cla-assistant` GitHub app.
