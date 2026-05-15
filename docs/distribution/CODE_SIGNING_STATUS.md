# Code Signing Status

Tracking sheet for the certificates and accounts that Block 5 (Tauri desktop) and Block 2 (CLI binaries) depend on. **Update weekly until all rows are green.**

Last updated: **2026-05-15**

---

## Status Matrix

| Platform | Identity | Status | Cost | ETA | Blocks |
|---|---|---|---|---|---|
| macOS Developer ID | Personal (`Nico Schindlbeck`) | ⬜ Not started | $99/yr | T+0 (24h after enrolment) | Block 5 final notarisation |
| Windows Code Signing | Azure Trusted Signing | ⬜ Not started | $9.99/mo | T+0 (instant after Azure setup) | Block 5 Windows installer |
| Linux Signing | minisign (self-managed) | ⬜ Not started | €0 | T+0 (15 min) | Block 5 AppImage/deb |
| Sigstore Cosign | Keyless OIDC via GitHub Actions | ⬜ Not started | €0 | T+0 (no enrolment) | Block 2 container images, Block 3 CLI |
| Apple Notary Service API key | Comes with Developer ID | ⬜ Blocked on row 1 | €0 | T+0 (after Developer ID) | Block 5 macOS auto-update |

---

## Decision Rationale

### macOS — Personal (not Organization)

We start with a **Personal** Apple Developer Program account, not an Organization. Gatekeeper will display "Developer ID: Nico Schindlbeck" on first launch. Reasoning:

- Organization enrolment requires a DUNS number AND an active legal entity. The `Plinth GmbH` referenced in the imprint is currently a placeholder. Real entity formation is months away (€500–2k cost + Notar).
- Apple supports a [Transfer to Organization](https://developer.apple.com/account/transfer-account/) flow once the legal entity exists. We can migrate the account without losing notarisation history.
- Personal enrolment completes within 24h after submission and payment.

When/if the GmbH exists, file a transfer ticket with Apple Support; cert chain re-issues automatically.

### Windows — Azure Trusted Signing (not USB-token EV cert)

We choose **Azure Trusted Signing** ($9.99/mo) over a traditional EV cert from SSL.com / GlobalSign / DigiCert (~€500/yr USB token). Reasoning:

- Microsoft-managed signing service. No physical USB token, no in-person identity verification appointment, no shipping delay.
- Sets a `MICROSOFT_TRUSTED_SIGNING` chain that bypasses SmartScreen reputation-gathering (the multi-week period where signed-but-unknown binaries trigger Defender warnings).
- Works natively in GitHub Actions via the [official task](https://github.com/Azure/trusted-signing-action).
- Cheaper at our scale.

Alternative (EV cert via SSL.com) kept as a fallback if Azure Trusted Signing approval is delayed.

### Linux — minisign

minisign is sufficient for our needs:

- AppImage and .deb packages signed with minisign are accepted by all major distros for non-repository installs.
- Many AppImages in the wild (Ledger Live, Obsidian, Inkscape) use minisign successfully.
- For repository installs (apt, dnf), we will revisit later when a stable user base exists. Out of scope for v1.6.

### Container images & CLI binaries — Sigstore cosign keyless

Cosign keyless via GitHub Actions OIDC is now industry-standard (Kubernetes, NPM, etc.):

- Zero private key material to manage.
- Identity bound to the workflow + repository, verified via Fulcio.
- Free and infinitely scalable.
- Verify command (note: pin OIDC issuer, NOT workflow path, to survive workflow renames):

```bash
cosign verify ghcr.io/<org>/plinth-<svc>:<tag> \
  --certificate-oidc-issuer "https://token.actions.githubusercontent.com" \
  --certificate-identity-regexp "^https://github.com/<org>/plinth/"
```

---

## Action Items — Owner Required (User)

These must be done by **you** since they require a credit card and personal/business identity:

### Today (T+0)

1. **Apple Developer Program** — https://developer.apple.com/programs/enroll/
   - [ ] Sign up with Apple ID (personal)
   - [ ] Pay $99/yr
   - [ ] Wait 24h for activation email
   - [ ] Once active: open Xcode → Settings → Accounts → add Apple ID → "Manage Certificates" → create "Developer ID Application" + "Developer ID Installer"
   - [ ] Export both certs as `.p12` to a secure password manager
   - [ ] Generate App-Specific Password for `notarytool` at https://appleid.apple.com → "App-Specific Passwords"
   - [ ] Update this doc when complete

2. **Azure Trusted Signing** — https://learn.microsoft.com/en-us/azure/trusted-signing/quickstart
   - [ ] Sign in to Azure portal with personal Microsoft account (or create one)
   - [ ] Activate $200 free trial credit if available
   - [ ] Create Trusted Signing Account in `westeurope` (or closest region)
   - [ ] Create Identity Validation request — choose "Public Trust" path
   - [ ] Submit identity documents (passport or Personalausweis scan)
   - [ ] Wait 1–7 days for validation
   - [ ] Once validated: create Certificate Profile → note the profile name
   - [ ] Update this doc when complete

3. **GitHub Cosign setup** — no enrolment, just workflow config
   - [ ] No action required from you. Block 2 PR will add `cosign-installer` step in the release workflow. Identity will be `https://github.com/<org>/plinth/...`.

### Week 1 (parallel)

4. **GitHub Organization** (Q2 from previous spec)
   - [ ] Decide: register `plinth` org on GitHub, or stay at `nico-schindlbeck-jpg`
   - [ ] If org: register at https://github.com/organizations/plan, link the repo
   - [ ] If staying: skip
   - [ ] Update Block 2 image refs in this repo to reflect chosen path

### Conditional (Block 5 final stretch)

5. **Notarisation App-Specific Password** — depends on Action Item 1
   - [ ] Generate and store in GitHub secrets as `APPLE_NOTARY_PASSWORD`
   - [ ] Also store `APPLE_TEAM_ID`, `APPLE_ID_EMAIL` in GitHub secrets

6. **Azure Trusted Signing GitHub Secrets** — depends on Action Item 2
   - [ ] Create service principal with `Trusted Signing Certificate Profile Signer` role
   - [ ] Store `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_ENDPOINT`, `AZURE_CODE_SIGNING_NAME`, `AZURE_CERT_PROFILE_NAME` in GitHub secrets

---

## What Happens If These Are Delayed

| Delay scenario | Impact on roadmap |
|---|---|
| Apple Developer enrolment > 1 week | Block 5 still proceeds; macOS bundle signs with ad-hoc cert in dev mode, notarisation deferred to release |
| Azure Trusted Signing > 2 weeks | Block 5 ships Windows installer with self-signed cert (will trigger SmartScreen until cert valid). Fallback: buy SSL.com EV cert (€500, 3-day USB token shipping) |
| GitHub Cosign integration issues | Block 2 ships container images unsigned; CLI verifies via sha256 pinning only. Add cosign in a follow-up patch release |
| GitHub Org not registered | Block 2 uses `ghcr.io/nico-schindlbeck-jpg/plinth-<svc>` namespace. Migration to `plinth` org possible later via image retag |

None of these block the technical work in Blocks 6, 4, 3, 2 — only the final-release polish for Block 5.

---

## Cost Summary (steady-state)

| Item | Annual | Monthly | Notes |
|---|---|---|---|
| Apple Developer Program (Personal) | $99 | – | renews annually |
| Azure Trusted Signing | – | $9.99 | + per-signature fees beyond free tier |
| GitHub Cosign (keyless) | $0 | $0 | free forever |
| Linux minisign | $0 | $0 | self-managed |
| **Total** | **~$220/yr** | **~$18/mo** | Excludes GmbH formation if pursued later |

---

## Tracking Log

| Date | Event | Owner |
|---|---|---|
| 2026-05-15 | Doc created, items 1–4 action-listed | Agent |
| | Apple Developer enrolment submitted | User |
| | Apple Developer activation received | User |
| | Azure Trusted Signing identity submitted | User |
| | Azure Trusted Signing approved | User |
| | GitHub Org decision made | User |
| | First test-signed macOS build | Block 5 |
| | First test-signed Windows installer | Block 5 |
| | First cosign-signed container image | Block 2 |
