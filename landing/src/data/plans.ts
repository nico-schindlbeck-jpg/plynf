// Single source of truth for Plynf's subscription tiers + feature gating.
//
// ⚠ This MUST stay in sync with the enforcement point:
//   services/proxy/src/plinth_proxy/tier_gate.py  (the `TIERS` matrix)
// The proxy is what actually blocks a call; this file is what the website,
// pricing page, dashboard, and Connect flow render. If a limit changes in one,
// change it in the other. The numbers below mirror tier_gate.py verbatim.
//
// Design: gate on **volume + features**, never on *how* you integrate. Every
// integration type (proxy / MCP / SDK / no-code node / webhook) works on every
// tier, including Free — that's a deliberate adoption choice.

export type TierId = "free" | "pro" | "enterprise";

/** Machine-checkable feature flags — mirror tier_gate.py's allow_* booleans. */
export type GateKey =
  | "pii_redaction"
  | "audit_log"
  | "custom_rest"
  | "self_hosted_sso";

export interface Plan {
  id: TierId;
  name: string;
  blurb: string;
  price: string;
  cadence: string;
  accent?: boolean;
  cta: { label: string; href: string };
  // Hard limits — mirror tier_gate.py. null = unlimited / all.
  monthlyTokens: number | null;
  maxConnectors: number | null;
  maxTenants: number | null;
  auditRetentionDays: number; // 0 = none
}

export interface PlanFeature {
  label: string;
  /** Per-tier display value for the comparison table. */
  values: Record<TierId, string>;
  /** When set, this row is a hard gate the proxy enforces at `minTier`+. */
  gate?: { key: GateKey; minTier: TierId };
}

export const PLANS: Plan[] = [
  {
    id: "free",
    name: "Free",
    blurb: "For solo builders and OSS projects.",
    price: "$0",
    cadence: "forever",
    cta: { label: "Get started", href: "/signup" },
    monthlyTokens: 100_000,
    maxConnectors: 3,
    maxTenants: 1,
    auditRetentionDays: 0,
  },
  {
    id: "pro",
    name: "Pro",
    blurb: "For teams shipping production agents.",
    price: "$49",
    cadence: "per month",
    accent: true,
    cta: { label: "Subscribe", href: "/signup?plan=pro" },
    monthlyTokens: 5_000_000,
    maxConnectors: null, // all 8 shipped
    maxTenants: 10,
    auditRetentionDays: 90,
  },
  {
    id: "enterprise",
    name: "Enterprise",
    blurb: "For regulated industries and bigco.",
    price: "Talk to us",
    cadence: "annual contract",
    cta: { label: "Contact sales", href: "mailto:sales@plynf.com?subject=Plynf%20Enterprise" },
    monthlyTokens: null,
    maxConnectors: null,
    maxTenants: null,
    auditRetentionDays: 365 * 7,
  },
];

// One matrix powers both the pricing comparison table AND the in-app gate
// badges. Rows with a `gate` are enforced by the proxy; the rest are
// display-only (support, SLA, integration breadth).
export const FEATURES: PlanFeature[] = [
  {
    label: "Shaped tokens / mo",
    values: { free: "100k", pro: "5M", enterprise: "Unlimited" },
  },
  {
    label: "Connectors",
    values: { free: "3", pro: "All 8", enterprise: "All 8 + custom REST" },
  },
  {
    label: "Integration types",
    values: {
      free: "All (proxy · MCP · SDK · node · webhook)",
      pro: "All",
      enterprise: "All + self-hosted Helm",
    },
  },
  {
    label: "Savings dashboard",
    values: { free: "Yes", pro: "Yes + Postgres sink", enterprise: "Yes + Postgres sink" },
  },
  {
    label: "PII redaction",
    values: { free: "—", pro: "Yes", enterprise: "Yes" },
    gate: { key: "pii_redaction", minTier: "pro" },
  },
  {
    label: "Audit log",
    values: { free: "—", pro: "90-day retention", enterprise: "7-year retention" },
    gate: { key: "audit_log", minTier: "pro" },
  },
  {
    label: "Custom REST connectors",
    values: { free: "—", pro: "Yes", enterprise: "Yes" },
    gate: { key: "custom_rest", minTier: "pro" },
  },
  {
    label: "SSO (SAML / OIDC) + self-hosted",
    values: { free: "—", pro: "—", enterprise: "Yes" },
    gate: { key: "self_hosted_sso", minTier: "enterprise" },
  },
  {
    label: "Support",
    values: { free: "Community", pro: "Email (24h)", enterprise: "Dedicated Slack" },
  },
  {
    label: "SLA",
    values: { free: "None", pro: "99.5%", enterprise: "99.95%" },
  },
];

const TIER_ORDER: TierId[] = ["free", "pro", "enterprise"];

/** True if `have` is at least `need` in the tier hierarchy. */
export function tierAtLeast(have: TierId, need: TierId): boolean {
  return TIER_ORDER.indexOf(have) >= TIER_ORDER.indexOf(need);
}

/** Whether a gated feature is unlocked on a given tier. */
export function featureAllowed(key: GateKey, tier: TierId): boolean {
  const f = FEATURES.find((x) => x.gate?.key === key);
  return f?.gate ? tierAtLeast(tier, f.gate.minTier) : true;
}

/** The lowest tier that unlocks a gated feature (for upgrade CTAs). */
export function minTierFor(key: GateKey): TierId | null {
  return FEATURES.find((x) => x.gate?.key === key)?.gate?.minTier ?? null;
}

export function planFor(id: TierId): Plan {
  return PLANS.find((p) => p.id === id) ?? PLANS[0];
}
