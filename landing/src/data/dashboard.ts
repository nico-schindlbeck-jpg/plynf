/**
 * Plynf dashboard — mock data contract.
 *
 * Single source of truth for every dashboard section so the numbers stay
 * internally consistent (the savings hero on Overview matches Token Economics,
 * the agent that "recovered" on Overview is the same one on Agents, etc.).
 * Swap this module for live API calls later; the shapes are the contract.
 *
 * All money is EUR. Token counts are raw integers. "Saved" always means
 * raw_tokens - shaped_tokens, i.e. what Plynf's response-shaping removed before
 * it ever reached the model.
 */

// ─── Formatters ────────────────────────────────────────────────────────────

export const eur = (n: number, frac = 2): string =>
  new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "EUR",
    minimumFractionDigits: frac,
    maximumFractionDigits: frac,
  }).format(n);

export const pct = (n: number, frac = 1): string => `${n.toFixed(frac)}%`;

export const compact = (n: number): string =>
  new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(n);

export const num = (n: number): string => new Intl.NumberFormat("en-US").format(n);

// ─── Headline KPIs ───────────────────────────────────────────────────────────

export interface Kpis {
  reductionPct: number; // headline "with vs without" reduction
  rawTokensToday: number;
  shapedTokensToday: number;
  savedTokensToday: number;
  costTodayEur: number; // actual spend today (with Plynf)
  costWithoutTodayEur: number; // what it would have cost without Plynf
  savedTodayEur: number;
  savedMonthEur: number;
  savedAllTimeEur: number;
  activeAgents: number;
  totalAgents: number;
  activeWorkflows: number;
  dlqCount: number;
  alertsCount: number;
  cacheHitRate: number;
  toolCallsToday: number;
}

export const kpis: Kpis = {
  reductionPct: 71.8,
  rawTokensToday: 48_920_400,
  shapedTokensToday: 13_795_553,
  savedTokensToday: 35_124_847,
  costTodayEur: 41.37,
  costWithoutTodayEur: 146.76,
  savedTodayEur: 105.39,
  savedMonthEur: 2_487.12,
  savedAllTimeEur: 28_941.66,
  activeAgents: 11,
  totalAgents: 13,
  activeWorkflows: 6,
  dlqCount: 3,
  alertsCount: 1,
  cacheHitRate: 38.4,
  toolCallsToday: 9_214,
};

// ─── Agents ───────────────────────────────────────────────────────────────────

export type AgentStatus = "healthy" | "degraded" | "down" | "paused";

export interface Agent {
  id: string;
  name: string;
  env: "prod" | "staging" | "dev";
  model: string;
  status: AgentStatus;
  requestsToday: number;
  savedPct: number;
  costTodayEur: number;
  p95ms: number;
  errorRatePct: number;
  recoveredToday: number; // crash-recovered workflow runs
  dlq: number;
  owner: string;
  lastActivity: string;
}

export const agents: Agent[] = [
  { id: "agt_support", name: "Customer Support", env: "prod", model: "gpt-4o", status: "healthy", requestsToday: 3120, savedPct: 73.1, costTodayEur: 12.84, p95ms: 940, errorRatePct: 0.2, recoveredToday: 0, dlq: 0, owner: "platform", lastActivity: "12s ago" },
  { id: "agt_sales", name: "Sales Outreach", env: "prod", model: "claude-3-5-sonnet", status: "healthy", requestsToday: 1840, savedPct: 70.4, costTodayEur: 8.11, p95ms: 1120, errorRatePct: 0.4, recoveredToday: 1, dlq: 0, owner: "growth", lastActivity: "44s ago" },
  { id: "agt_research", name: "Market Research", env: "prod", model: "gpt-4o", status: "degraded", requestsToday: 962, savedPct: 68.9, costTodayEur: 6.92, p95ms: 2310, errorRatePct: 2.1, recoveredToday: 2, dlq: 2, owner: "platform", lastActivity: "3m ago" },
  { id: "agt_billing", name: "Billing Ops", env: "prod", model: "groq/llama-3.3-70b", status: "healthy", requestsToday: 540, savedPct: 74.8, costTodayEur: 1.32, p95ms: 410, errorRatePct: 0.1, recoveredToday: 0, dlq: 0, owner: "finance", lastActivity: "1m ago" },
  { id: "agt_devrel", name: "DevRel Assistant", env: "prod", model: "gpt-4o-mini", status: "healthy", requestsToday: 1290, savedPct: 71.2, costTodayEur: 2.04, p95ms: 680, errorRatePct: 0.3, recoveredToday: 0, dlq: 0, owner: "devrel", lastActivity: "8s ago" },
  { id: "agt_triage", name: "Incident Triage", env: "prod", model: "claude-3-5-sonnet", status: "healthy", requestsToday: 410, savedPct: 69.7, costTodayEur: 3.41, p95ms: 1480, errorRatePct: 0.6, recoveredToday: 0, dlq: 0, owner: "platform", lastActivity: "22s ago" },
  { id: "agt_dataeng", name: "Data Enrichment", env: "prod", model: "gpt-4o", status: "down", requestsToday: 0, savedPct: 0, costTodayEur: 0.0, p95ms: 0, errorRatePct: 0, recoveredToday: 1, dlq: 1, owner: "platform", lastActivity: "18m ago" },
  { id: "agt_legal", name: "Contract Review", env: "prod", model: "claude-3-5-sonnet", status: "healthy", requestsToday: 88, savedPct: 72.6, costTodayEur: 1.97, p95ms: 1990, errorRatePct: 0.0, recoveredToday: 0, dlq: 0, owner: "legal", lastActivity: "6m ago" },
  { id: "agt_onboard", name: "User Onboarding", env: "prod", model: "gpt-4o-mini", status: "healthy", requestsToday: 760, savedPct: 70.9, costTodayEur: 0.98, p95ms: 520, errorRatePct: 0.2, recoveredToday: 0, dlq: 0, owner: "growth", lastActivity: "35s ago" },
  { id: "agt_qa", name: "QA Copilot", env: "staging", model: "gpt-4o", status: "healthy", requestsToday: 230, savedPct: 71.5, costTodayEur: 0.74, p95ms: 870, errorRatePct: 0.5, recoveredToday: 0, dlq: 0, owner: "eng", lastActivity: "2m ago" },
  { id: "agt_translate", name: "Localization", env: "prod", model: "groq/llama-3.3-70b", status: "healthy", requestsToday: 1420, savedPct: 73.9, costTodayEur: 0.61, p95ms: 300, errorRatePct: 0.1, recoveredToday: 0, dlq: 0, owner: "intl", lastActivity: "5s ago" },
  { id: "agt_scheduler", name: "Meeting Scheduler", env: "prod", model: "gpt-4o-mini", status: "paused", requestsToday: 0, savedPct: 70.1, costTodayEur: 0.0, p95ms: 0, errorRatePct: 0, recoveredToday: 0, dlq: 0, owner: "ops", lastActivity: "2h ago" },
  { id: "agt_voice", name: "Voice IVR", env: "dev", model: "gpt-4o", status: "healthy", requestsToday: 64, savedPct: 67.8, costTodayEur: 0.38, p95ms: 1310, errorRatePct: 0.8, recoveredToday: 0, dlq: 0, owner: "eng", lastActivity: "9m ago" },
];

// ─── Workflows (with checkpoints / crash-recovery) ──────────────────────────

export type WorkflowStatus = "running" | "completed" | "recovered" | "failed" | "queued";
export type StepStatus = "done" | "active" | "pending" | "failed" | "checkpoint";

export interface WorkflowStep {
  name: string;
  status: StepStatus;
  durationMs?: number;
  checkpoint?: boolean;
}

export interface Workflow {
  id: string;
  agentId: string;
  agent: string;
  name: string;
  status: WorkflowStatus;
  progress: number; // 0..100
  startedAgo: string;
  steps: WorkflowStep[];
  recoveredFrom?: string; // last good checkpoint, if recovered
}

export const workflows: Workflow[] = [
  {
    id: "wf_8c21", agentId: "agt_support", agent: "Customer Support", name: "Resolve ticket #48213",
    status: "running", progress: 62, startedAgo: "1m 12s ago",
    steps: [
      { name: "Classify intent", status: "done", durationMs: 220 },
      { name: "Fetch order (tool)", status: "done", durationMs: 480, checkpoint: true },
      { name: "Check refund policy", status: "done", durationMs: 310 },
      { name: "Draft reply", status: "active" },
      { name: "Human approval", status: "pending" },
      { name: "Send", status: "pending" },
    ],
  },
  {
    id: "wf_5f90", agentId: "agt_research", agent: "Market Research", name: "Competitor scan — Q2",
    status: "recovered", progress: 48, startedAgo: "6m ago", recoveredFrom: "Aggregate sources",
    steps: [
      { name: "Plan queries", status: "done", durationMs: 190 },
      { name: "Web search (tool)", status: "done", durationMs: 2200 },
      { name: "Aggregate sources", status: "checkpoint", durationMs: 1400, checkpoint: true },
      { name: "Summarize", status: "active" },
      { name: "Score & rank", status: "pending" },
    ],
  },
  {
    id: "wf_2a18", agentId: "agt_dataeng", agent: "Data Enrichment", name: "Enrich 1,204 leads",
    status: "failed", progress: 31, startedAgo: "18m ago", recoveredFrom: "Validate schema",
    steps: [
      { name: "Load batch", status: "done", durationMs: 540 },
      { name: "Validate schema", status: "checkpoint", durationMs: 300, checkpoint: true },
      { name: "Enrich (CRM tool)", status: "failed" },
      { name: "Write back", status: "pending" },
    ],
  },
  {
    id: "wf_91bd", agentId: "agt_sales", agent: "Sales Outreach", name: "Sequence — enterprise tier",
    status: "running", progress: 80, startedAgo: "44s ago",
    steps: [
      { name: "Segment list", status: "done", durationMs: 160 },
      { name: "Personalize (tool)", status: "done", durationMs: 900, checkpoint: true },
      { name: "Compliance check", status: "done", durationMs: 240 },
      { name: "Schedule send", status: "active" },
    ],
  },
  {
    id: "wf_77e3", agentId: "agt_legal", agent: "Contract Review", name: "MSA redline — Acme",
    status: "completed", progress: 100, startedAgo: "6m ago",
    steps: [
      { name: "Parse document", status: "done", durationMs: 1100 },
      { name: "Clause extraction", status: "done", durationMs: 1900, checkpoint: true },
      { name: "Risk scoring", status: "done", durationMs: 800 },
      { name: "Generate redline", status: "done", durationMs: 1500 },
    ],
  },
  {
    id: "wf_03aa", agentId: "agt_triage", agent: "Incident Triage", name: "PagerDuty INC-7782",
    status: "running", progress: 25, startedAgo: "22s ago",
    steps: [
      { name: "Ingest alert", status: "done", durationMs: 90 },
      { name: "Correlate logs (tool)", status: "active" },
      { name: "Hypothesize cause", status: "pending" },
      { name: "Propose remediation", status: "pending" },
    ],
  },
  {
    id: "wf_64c7", agentId: "agt_billing", agent: "Billing Ops", name: "Reconcile invoices",
    status: "queued", progress: 0, startedAgo: "queued",
    steps: [
      { name: "Pull Stripe events", status: "pending" },
      { name: "Match to ledger", status: "pending" },
      { name: "Flag deltas", status: "pending" },
    ],
  },
];

// ─── Savings time series (30 days) ──────────────────────────────────────────

export interface SavingsPoint {
  day: number; // 1..30
  label: string; // e.g. "May 12"
  withoutEur: number;
  withEur: number;
}

const _months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun"];
export const savingsSeries: SavingsPoint[] = Array.from({ length: 30 }, (_, i) => {
  const day = i + 1;
  // Gentle growth + weekly ripple; deterministic (no Math.random for stable build).
  const base = 96 + i * 3.4;
  const ripple = Math.sin(i / 4.5) * 14 + Math.cos(i / 2.2) * 6;
  const without = Math.max(40, base + ripple);
  const reduction = 0.7 + (Math.sin(i / 6) + 1) * 0.012; // ~70–72.4%
  const withP = without * (1 - reduction);
  const date = new Date(2026, 4, day); // May 2026
  return {
    day,
    label: `${_months[date.getMonth()]} ${day}`,
    withoutEur: Math.round(without * 100) / 100,
    withEur: Math.round(withP * 100) / 100,
  };
});

export const savingsTotals = {
  withoutMonthEur: savingsSeries.reduce((s, p) => s + p.withoutEur, 0),
  withMonthEur: savingsSeries.reduce((s, p) => s + p.withEur, 0),
  get savedMonthEur() {
    return this.withoutMonthEur - this.withMonthEur;
  },
};

// ─── Work summaries (natural language) ──────────────────────────────────────

export interface WorkSummary {
  id: string;
  agent: string;
  agentId: string;
  when: string;
  title: string;
  body: string;
  tools: string[];
  sources: string[];
  result: string;
  tokensSavedPct: number;
}

export const summaries: WorkSummary[] = [
  {
    id: "sum_1", agent: "Customer Support", agentId: "agt_support", when: "Today, 14:32",
    title: "Resolved a refund dispute for order #48213",
    body: "The agent classified the customer's message as a refund request, looked up the order, confirmed it fell inside the 30-day window, and drafted an empathetic reply offering a full refund. It flagged the response for human approval because the order value exceeded €200.",
    tools: ["orders.lookup", "policy.refunds", "email.draft"],
    sources: ["Order #48213", "Refund policy v3", "Customer history"],
    result: "Draft refund approved by agent; awaiting human sign-off.",
    tokensSavedPct: 73.1,
  },
  {
    id: "sum_2", agent: "Market Research", agentId: "agt_research", when: "Today, 14:05",
    title: "Compiled a Q2 competitor landscape brief",
    body: "Ran eight targeted web searches, de-duplicated 40 sources down to 12 high-signal ones, and produced a one-page brief covering pricing moves, new features, and hiring signals across four competitors. The run crashed mid-summary and was resumed from the last checkpoint without losing the aggregated sources.",
    tools: ["web.search", "web.fetch", "doc.summarize"],
    sources: ["12 web sources", "Crunchbase", "Internal pricing sheet"],
    result: "1-page brief delivered; recovered cleanly after a transient crash.",
    tokensSavedPct: 68.9,
  },
  {
    id: "sum_3", agent: "Contract Review", agentId: "agt_legal", when: "Today, 13:48",
    title: "Redlined the Acme MSA and scored 6 risk clauses",
    body: "Parsed a 22-page master service agreement, extracted indemnity, liability-cap, and data-processing clauses, scored each against the company playbook, and generated a redline with suggested counter-language for the two highest-risk clauses.",
    tools: ["doc.parse", "clause.extract", "redline.generate"],
    sources: ["Acme MSA.pdf", "Legal playbook 2026"],
    result: "Redline ready for counsel review; 2 clauses flagged high-risk.",
    tokensSavedPct: 72.6,
  },
  {
    id: "sum_4", agent: "Incident Triage", agentId: "agt_triage", when: "Today, 13:20",
    title: "Triaged PagerDuty INC-7782 (elevated 5xx on checkout)",
    body: "Correlated a spike in 5xx errors with a recent deploy, identified a likely null-pointer in the payment adapter from the stack traces, and proposed a rollback plus a one-line guard. Posted findings to the incident channel within 40 seconds of the page.",
    tools: ["logs.query", "deploys.list", "slack.post"],
    sources: ["Datadog logs", "Deploy #4821", "Runbook: checkout"],
    result: "Rollback recommended; on-call accepted the remediation.",
    tokensSavedPct: 69.7,
  },
  {
    id: "sum_5", agent: "Sales Outreach", agentId: "agt_sales", when: "Today, 12:55",
    title: "Personalized an enterprise outreach sequence (42 accounts)",
    body: "Segmented the target list by industry and headcount, generated a personalized three-touch sequence per account grounded in recent funding and product news, and ran every draft through the compliance checker before scheduling.",
    tools: ["crm.segment", "news.lookup", "compliance.check"],
    sources: ["Salesforce", "42 account profiles", "Compliance ruleset"],
    result: "126 messages scheduled across 42 accounts; 0 compliance flags.",
    tokensSavedPct: 70.4,
  },
];

// ─── Tool Gateway ────────────────────────────────────────────────────────────

export type ToolStatus = "connected" | "degraded" | "error";

export interface GatewayTool {
  name: string;
  category: string;
  callsToday: number;
  cacheHitRate: number;
  costTodayEur: number;
  p95ms: number;
  status: ToolStatus;
}

export const gatewayTools: GatewayTool[] = [
  { name: "orders.lookup", category: "Commerce", callsToday: 2140, cacheHitRate: 52.1, costTodayEur: 0.0, p95ms: 180, status: "connected" },
  { name: "web.search", category: "Research", callsToday: 1860, cacheHitRate: 21.4, costTodayEur: 9.18, p95ms: 1240, status: "connected" },
  { name: "web.fetch", category: "Research", callsToday: 1530, cacheHitRate: 44.0, costTodayEur: 0.0, p95ms: 760, status: "connected" },
  { name: "crm.segment", category: "Sales", callsToday: 612, cacheHitRate: 33.8, costTodayEur: 0.0, p95ms: 420, status: "connected" },
  { name: "slack.post", category: "Comms", callsToday: 980, cacheHitRate: 0.0, costTodayEur: 0.0, p95ms: 260, status: "connected" },
  { name: "logs.query", category: "Observability", callsToday: 410, cacheHitRate: 12.0, costTodayEur: 0.0, p95ms: 1510, status: "degraded" },
  { name: "doc.parse", category: "Documents", callsToday: 96, cacheHitRate: 8.3, costTodayEur: 0.0, p95ms: 2200, status: "connected" },
  { name: "stripe.events", category: "Finance", callsToday: 540, cacheHitRate: 61.2, costTodayEur: 0.0, p95ms: 340, status: "connected" },
  { name: "crm.enrich", category: "Sales", callsToday: 88, cacheHitRate: 0.0, costTodayEur: 0.0, p95ms: 0, status: "error" },
];

// ─── Audit log (tamper-evident, hash-chained) ───────────────────────────────

export type AuditDecision = "allow" | "deny" | "shaped";

export interface AuditEntry {
  ts: string;
  actor: string;
  tenant: string;
  action: string;
  tool: string;
  decision: AuditDecision;
  hash: string; // chained hash prefix
}

export const auditLog: AuditEntry[] = [
  { ts: "14:32:08", actor: "agt_support", tenant: "acme", action: "tool.invoke", tool: "orders.lookup", decision: "allow", hash: "a91f…3c2" },
  { ts: "14:32:07", actor: "agt_support", tenant: "acme", action: "response.shape", tool: "orders.lookup", decision: "shaped", hash: "7d10…8b4" },
  { ts: "14:31:55", actor: "agt_research", tenant: "globex", action: "tool.invoke", tool: "web.search", decision: "allow", hash: "c4e2…1aa" },
  { ts: "14:31:40", actor: "agt_dataeng", tenant: "acme", action: "tool.invoke", tool: "crm.enrich", decision: "deny", hash: "0f88…d52" },
  { ts: "14:31:22", actor: "user:lena@acme", tenant: "acme", action: "token.rotate", tool: "—", decision: "allow", hash: "b772…9e1" },
  { ts: "14:30:58", actor: "agt_sales", tenant: "globex", action: "tool.invoke", tool: "compliance.check", decision: "allow", hash: "5a3c…77f" },
  { ts: "14:30:31", actor: "agt_legal", tenant: "acme", action: "data.export", tool: "doc.parse", decision: "shaped", hash: "e019…4b0" },
  { ts: "14:30:12", actor: "agt_triage", tenant: "acme", action: "tool.invoke", tool: "logs.query", decision: "allow", hash: "2cc8…a13" },
];

// ─── Capability tokens ───────────────────────────────────────────────────────

export interface CapabilityToken {
  id: string;
  name: string;
  scopes: string[];
  tenant: string;
  createdAgo: string;
  rotatedAgo: string;
  lastUsed: string;
  status: "active" | "rotate-soon" | "expired";
}

export const capabilityTokens: CapabilityToken[] = [
  { id: "tok_prod_01", name: "prod-support", scopes: ["tool:orders:read", "tool:email:write"], tenant: "acme", createdAgo: "94d ago", rotatedAgo: "12d ago", lastUsed: "12s ago", status: "active" },
  { id: "tok_prod_02", name: "prod-research", scopes: ["tool:web:read"], tenant: "globex", createdAgo: "210d ago", rotatedAgo: "88d ago", lastUsed: "3m ago", status: "rotate-soon" },
  { id: "tok_ci_01", name: "ci-deploy", scopes: ["workflow:deploy"], tenant: "acme", createdAgo: "30d ago", rotatedAgo: "30d ago", lastUsed: "1d ago", status: "active" },
  { id: "tok_legacy", name: "legacy-import", scopes: ["tool:crm:write"], tenant: "acme", createdAgo: "400d ago", rotatedAgo: "400d ago", lastUsed: "62d ago", status: "expired" },
];

// ─── Team & tenants ────────────────────────────────────────────────────────

export interface TeamMember {
  name: string;
  email: string;
  role: "Owner" | "Admin" | "Developer" | "Finance" | "Viewer";
  tenant: string;
  lastActive: string;
}

export const team: TeamMember[] = [
  { name: "Lena Hofer", email: "lena@acme.io", role: "Owner", tenant: "acme", lastActive: "now" },
  { name: "Marco Ruiz", email: "marco@acme.io", role: "Admin", tenant: "acme", lastActive: "8m ago" },
  { name: "Priya Nair", email: "priya@acme.io", role: "Developer", tenant: "acme", lastActive: "1h ago" },
  { name: "Tom Becker", email: "tom@acme.io", role: "Finance", tenant: "acme", lastActive: "3h ago" },
  { name: "Sofia Lind", email: "sofia@globex.com", role: "Admin", tenant: "globex", lastActive: "2d ago" },
];

export interface Tenant {
  id: string;
  name: string;
  isolation: "dedicated" | "pooled";
  region: string;
  agents: number;
  monthlySpendEur: number;
}

export const tenants: Tenant[] = [
  { id: "acme", name: "Acme Inc.", isolation: "dedicated", region: "eu-central-1", agents: 9, monthlySpendEur: 1284.4 },
  { id: "globex", name: "Globex Corp.", isolation: "pooled", region: "eu-west-1", agents: 4, monthlySpendEur: 612.8 },
];

// ─── Rate limits & cost caps (per agent, sampled) ───────────────────────────

export interface Guardrail {
  agentId: string;
  agent: string;
  rpmLimit: number;
  rpmCurrent: number;
  dailyCapEur: number;
  spentTodayEur: number;
}

export const guardrails: Guardrail[] = [
  { agentId: "agt_support", agent: "Customer Support", rpmLimit: 120, rpmCurrent: 78, dailyCapEur: 50, spentTodayEur: 12.84 },
  { agentId: "agt_research", agent: "Market Research", rpmLimit: 60, rpmCurrent: 54, dailyCapEur: 25, spentTodayEur: 6.92 },
  { agentId: "agt_sales", agent: "Sales Outreach", rpmLimit: 90, rpmCurrent: 31, dailyCapEur: 40, spentTodayEur: 8.11 },
  { agentId: "agt_triage", agent: "Incident Triage", rpmLimit: 200, rpmCurrent: 12, dailyCapEur: 30, spentTodayEur: 3.41 },
];

// ─── Billing ─────────────────────────────────────────────────────────────────

export interface Invoice {
  id: string;
  period: string;
  amountEur: number;
  status: "paid" | "open" | "void";
}

export const billing = {
  plan: "Scale",
  priceModel: "Usage-based · €0.85 / 1M shaped tokens",
  includedTokensM: 50,
  usedTokensM: 41.3,
  cycleResetsIn: "9 days",
  currentBillEur: 187.4,
  projectedBillEur: 224.0,
  paymentMethod: "Visa •••• 4242",
  invoices: <Invoice[]>[
    { id: "inv_2026_05", period: "May 2026", amountEur: 224.0, status: "open" },
    { id: "inv_2026_04", period: "Apr 2026", amountEur: 198.7, status: "paid" },
    { id: "inv_2026_03", period: "Mar 2026", amountEur: 176.2, status: "paid" },
    { id: "inv_2026_02", period: "Feb 2026", amountEur: 154.9, status: "paid" },
  ],
};

// ─── Upstream routing & providers — THE core product configuration ──────────
// Plynf is a control plane: a tenant operates the proxy entirely from here.
// These map 1:1 to PLINTH_PROXY_PROVIDERS / MODEL_ALIASES / front-door routes.

export interface Provider {
  id: string;
  name: string;
  baseUrl: string;
  keyMasked: string;
  headers: { k: string; v: string }[];
  models: number;
  isDefault: boolean;
  status: "connected" | "degraded" | "error";
}

export const providers: Provider[] = [
  { id: "openai", name: "OpenAI", baseUrl: "https://api.openai.com", keyMasked: "sk-…7Qd2", headers: [], models: 34, isDefault: true, status: "connected" },
  { id: "anthropic", name: "Anthropic", baseUrl: "https://api.anthropic.com", keyMasked: "sk-ant-…b91", headers: [], models: 8, isDefault: false, status: "connected" },
  { id: "groq", name: "Groq", baseUrl: "https://api.groq.com/openai", keyMasked: "gsk_…f0a", headers: [], models: 12, isDefault: false, status: "connected" },
  { id: "azure", name: "Azure OpenAI", baseUrl: "https://acme.openai.azure.com", keyMasked: "—", headers: [{ k: "api-key", v: "${AZURE_KEY}" }], models: 6, isDefault: false, status: "degraded" },
  { id: "openrouter", name: "OpenRouter", baseUrl: "https://openrouter.ai/api", keyMasked: "sk-or-…12c", headers: [{ k: "HTTP-Referer", v: "https://acme.io" }, { k: "X-Title", v: "Acme" }], models: 180, isDefault: false, status: "connected" },
];

export interface ModelAlias {
  alias: string;
  target: string;
}

export const modelAliases: ModelAlias[] = [
  { alias: "fast", target: "groq/llama-3.3-70b" },
  { alias: "smart", target: "openai/gpt-4o" },
  { alias: "cheap", target: "openai/gpt-4o-mini" },
  { alias: "long-context", target: "anthropic/claude-3-5-sonnet" },
];

export interface FrontDoor {
  id: string;
  name: string;
  path: string;
  enabled: boolean;
  desc: string;
}

export const frontDoors: FrontDoor[] = [
  { id: "openai", name: "OpenAI Chat", path: "/v1/chat/completions", enabled: true, desc: "Point any OpenAI SDK's base_url here." },
  { id: "anthropic", name: "Anthropic Messages", path: "/v1/messages", enabled: true, desc: "Anthropic SDK + count_tokens." },
  { id: "gemini", name: "Google Gemini", path: "/v1beta/models/*:generateContent", enabled: true, desc: "Gemini generateContent + Vertex." },
  { id: "cohere", name: "Cohere v2", path: "/v2/chat", enabled: true, desc: "Cohere v2 chat." },
  { id: "responses", name: "OpenAI Responses", path: "/v1/responses", enabled: true, desc: "The Responses API surface." },
  { id: "bedrock", name: "Bedrock Converse", path: "/model/*/converse", enabled: false, desc: "AWS Bedrock Converse dialect." },
  { id: "ollama", name: "Ollama (native)", path: "/api/chat · /api/generate", enabled: true, desc: "Native Ollama clients & tools." },
];

// ─── Onboarding / setup checklist (persisted client-side) ───────────────────

export interface OnboardStep {
  id: string;
  title: string;
  desc: string;
  code?: string;
  lang?: string;
  done: boolean;
}

export const onboarding: OnboardStep[] = [
  { id: "install", title: "Install Plynf", desc: "One command spins up the proxy, gateway, workspace and identity services locally.", code: "curl -fsSL https://plynf.com/install.sh | sh", lang: "bash", done: true },
  { id: "key", title: "Create an API key", desc: "Mint a tenant key the proxy accepts. You can scope and rotate it any time under Settings & Safety.", code: "plynf keys create --tenant acme --tier scale", lang: "bash", done: true },
  { id: "point", title: "Point your SDK at Plynf", desc: "Swap your base URL — no code change beyond one line. Every OpenAI-compatible SDK just works.", code: "from openai import OpenAI\nclient = OpenAI(\n    base_url=\"https://app.plynf.com/v1\",\n    api_key=\"plynf_sk_…\",\n)", lang: "python", done: false },
  { id: "tool", title: "Connect your first tool", desc: "Add a connector in Tool Gateway (OAuth). Plynf shapes its responses before they reach the model.", done: false },
  { id: "shape", title: "Set a response-shaping policy", desc: "Keep only the fields your agent needs. A 150-field object becomes 8 — that's where the 71% comes from.", done: false },
  { id: "savings", title: "See your first savings", desc: "Send a request and watch the Overview hero update. Most teams see 68–74% token reduction on day one.", done: false },
];

export interface BestPractice {
  icon: string;
  tag: string;
  title: string;
  body: string;
}

export const bestPractices: BestPractice[] = [
  { icon: "coins", tag: "Cost", title: "Alias models, don't hard-code them", body: "Reference `fast` / `smart` in code and swap the provider behind the alias from this dashboard — no redeploy. Route bulk work to a cheap model and only escalate when needed." },
  { icon: "plug", tag: "Shaping", title: "Keep the fields, drop the noise", body: "Most tool responses are 90% metadata. Set a keep-list per tool so a 2,000-token order object arrives as ~80 tokens. This is the single biggest lever on cost." },
  { icon: "shield", tag: "Safety", title: "Scope tokens, rotate on a schedule", body: "Give each agent a capability token scoped to exactly the tools and workspace it needs. Keep RS256 with 30-day rotation on; revoke instantly if an agent misbehaves." },
  { icon: "agents", tag: "Reliability", title: "Make long jobs durable", body: "Run multi-step work as a checkpointed workflow. If a worker dies, Plynf resumes from the last checkpoint instead of restarting — and bad messages land in the DLQ for replay." },
  { icon: "coins", tag: "Cost", title: "Set per-agent cost caps", body: "A runaway loop shouldn't cost you €1,000. Cap hourly and daily spend per agent; Plynf blocks over-budget calls and alerts you before the cap, not after." },
  { icon: "plug", tag: "Routing", title: "Front more than one provider", body: "Configure several upstreams and route per request. Use the header override for ad-hoc tests, the `provider/model` prefix for explicit routing, and aliases for the default path." },
];

// ─── Response-shaping policies (per tool) ───────────────────────────────────

export interface ToolPolicy {
  tool: string;
  keepFields: string[];
  maxTokens: number;
  cacheTtlS: number;
  blockWrites: boolean;
  reductionPct: number;
}

export const toolPolicies: ToolPolicy[] = [
  { tool: "salesforce.get_order", keepFields: ["id", "status", "total", "items[].sku", "items[].qty", "customer.email", "created", "currency"], maxTokens: 256, cacheTtlS: 300, blockWrites: true, reductionPct: 96.2 },
  { tool: "github.list_issues", keepFields: ["number", "title", "labels", "state", "assignee"], maxTokens: 512, cacheTtlS: 60, blockWrites: false, reductionPct: 71.0 },
  { tool: "web.fetch", keepFields: ["title", "text", "url"], maxTokens: 1024, cacheTtlS: 600, blockWrites: false, reductionPct: 64.0 },
  { tool: "slack.history", keepFields: ["messages[].user", "messages[].text", "messages[].ts"], maxTokens: 400, cacheTtlS: 30, blockWrites: false, reductionPct: 58.4 },
];

// ─── Nav model (shared by the dashboard shell) ──────────────────────────────

export interface NavItem {
  href: string;
  label: string;
  icon: string; // key resolved by Icon.astro
  badge?: number;
  group: "operate" | "configure" | "account";
}

export const dashboardNav: NavItem[] = [
  { href: "/app", label: "Overview", icon: "home", group: "operate" },
  { href: "/app/agents", label: "Agents & Workflows", icon: "agents", badge: kpis.dlqCount, group: "operate" },
  { href: "/app/summaries", label: "Work Summaries", icon: "sparkles", group: "operate" },
  { href: "/app/economics", label: "Token Economics", icon: "coins", group: "operate" },
  { href: "/app/routing", label: "Routing & Providers", icon: "route", group: "configure" },
  { href: "/app/gateway", label: "Tool Gateway", icon: "plug", group: "configure" },
  { href: "/app/settings", label: "Settings & Safety", icon: "shield", group: "configure" },
  { href: "/app/setup", label: "Setup & Best Practices", icon: "rocket", group: "configure" },
  { href: "/app/billing", label: "Account & Billing", icon: "card", group: "account" },
];

export const navGroups: { id: "operate" | "configure" | "account"; label: string }[] = [
  { id: "operate", label: "Operate" },
  { id: "configure", label: "Configure & control" },
  { id: "account", label: "Account" },
];
