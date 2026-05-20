/**
 * First-Run Wizard — three steps, ≤ 3 clicks.
 *
 * Block 4 acceptance criterion: a fresh Plinth instance opens /welcome,
 * user picks a workspace name, optionally pastes LLM keys (or selects
 * Mock Mode), clicks "Start", sees the demo card. ≤ 5 min wall-clock
 * end-to-end.
 */

import { useState } from "preact/hooks";
import { ApiError, api } from "../lib/api";

type Step = 1 | 2 | 3 | "complete";

interface WizardState {
  step: Step;
  tenantName: string;
  llmAnthropic: string;
  llmOpenai: string;
  mockMode: boolean;
  apiKey: string | null;
  error: string | null;
  remediation: string | null;
  submitting: boolean;
}

const initialState: WizardState = {
  step: 1,
  tenantName: defaultTenantName(),
  llmAnthropic: "",
  llmOpenai: "",
  mockMode: false,
  apiKey: null,
  error: null,
  remediation: null,
  submitting: false,
};

function defaultTenantName(): string {
  if (typeof navigator !== "undefined" && navigator.userAgent.includes("Mac")) {
    return "my-workspace";
  }
  return "my-workspace";
}

export function WelcomeWizard() {
  const [s, set] = useState<WizardState>(initialState);

  const next = () => set({ ...s, step: (s.step === 3 ? "complete" : ((s.step as number) + 1)) as Step });
  const prev = () => set({ ...s, step: Math.max(1, (s.step as number) - 1) as Step });

  const onStart = async () => {
    set({ ...s, submitting: true, error: null, remediation: null });
    try {
      const result = await api.bootstrap({
        tenant_name: s.tenantName,
        llm_keys: s.mockMode
          ? undefined
          : {
              ...(s.llmAnthropic ? { anthropic: s.llmAnthropic } : {}),
              ...(s.llmOpenai ? { openai: s.llmOpenai } : {}),
            },
        mock_mode: s.mockMode,
      });
      set({ ...s, apiKey: result.api_key, step: "complete", submitting: false });
    } catch (e) {
      const err = e instanceof ApiError ? e : new Error("Bootstrap failed");
      set({
        ...s,
        submitting: false,
        error: err.message,
        remediation: e instanceof ApiError ? (e.remediation ?? null) : null,
      });
    }
  };

  return (
    <div class="wizard">
      <header class="wizard-header">
        <Logo />
        <span class="wizard-version">v1.7 · API v1 stable</span>
      </header>

      <main class="wizard-main">
        <div class="wizard-progress" role="progressbar">
          {[1, 2, 3].map((n) => (
            <div
              key={n}
              class={`wizard-progress-step ${s.step === "complete" || (s.step as number) >= n ? "is-active" : ""}`}
            />
          ))}
        </div>

        {s.step === 1 && (
          <Step1
            tenantName={s.tenantName}
            onChange={(v) => set({ ...s, tenantName: v })}
            onNext={next}
          />
        )}
        {s.step === 2 && (
          <Step2
            llmAnthropic={s.llmAnthropic}
            llmOpenai={s.llmOpenai}
            mockMode={s.mockMode}
            onChange={(patch) => set({ ...s, ...patch })}
            onBack={prev}
            onNext={next}
          />
        )}
        {s.step === 3 && (
          <Step3
            tenantName={s.tenantName}
            mockMode={s.mockMode}
            llmConfigured={Boolean(s.llmAnthropic || s.llmOpenai)}
            submitting={s.submitting}
            error={s.error}
            remediation={s.remediation}
            onBack={prev}
            onStart={onStart}
          />
        )}
        {s.step === "complete" && s.apiKey && (
          <Complete tenantName={s.tenantName} apiKey={s.apiKey} />
        )}
      </main>

      <footer class="wizard-footer">
        <span>Need help? · </span>
        <a href="https://docs.plinth.dev/wizard" target="_blank" rel="noopener">
          docs.plinth.dev/wizard
        </a>
      </footer>
    </div>
  );
}

// ─── Step components ──────────────────────────────────────────────

function Step1({
  tenantName,
  onChange,
  onNext,
}: {
  tenantName: string;
  onChange: (v: string) => void;
  onNext: () => void;
}) {
  const valid = /^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$/.test(tenantName);
  return (
    <section class="wizard-step">
      <p class="wizard-eyebrow">Step 1 of 3</p>
      <h1 class="wizard-title">Name your workspace.</h1>
      <p class="wizard-lead">
        One per project. Lowercase letters, numbers, and hyphens. You can add more later.
      </p>
      <label class="wizard-field">
        <span>Workspace name</span>
        <input
          type="text"
          value={tenantName}
          onInput={(e) => onChange((e.target as HTMLInputElement).value)}
          placeholder="my-research"
          autoFocus
          spellcheck={false}
          autoComplete="off"
        />
        {!valid && tenantName.length > 0 && (
          <span class="wizard-field-error">
            Use 3-40 lowercase letters, numbers, or hyphens. Cannot start or end with a hyphen.
          </span>
        )}
      </label>
      <div class="wizard-actions">
        <span />
        <button class="wizard-btn-primary" disabled={!valid} onClick={onNext}>
          Continue →
        </button>
      </div>
    </section>
  );
}

function Step2({
  llmAnthropic,
  llmOpenai,
  mockMode,
  onChange,
  onBack,
  onNext,
}: {
  llmAnthropic: string;
  llmOpenai: string;
  mockMode: boolean;
  onChange: (patch: Partial<{ llmAnthropic: string; llmOpenai: string; mockMode: boolean }>) => void;
  onBack: () => void;
  onNext: () => void;
}) {
  return (
    <section class="wizard-step">
      <p class="wizard-eyebrow">Step 2 of 3</p>
      <h1 class="wizard-title">LLM keys, or skip.</h1>
      <p class="wizard-lead">
        Stored encrypted at rest. Used only for agent calls you initiate. Skip to use Mock Mode
        — every tool returns deterministic fixtures, no model calls, $0 cost.
      </p>

      <label class={`wizard-toggle ${mockMode ? "is-active" : ""}`}>
        <input
          type="checkbox"
          checked={mockMode}
          onChange={(e) => onChange({ mockMode: (e.target as HTMLInputElement).checked })}
        />
        <span class="wizard-toggle-track">
          <span class="wizard-toggle-thumb" />
        </span>
        <span class="wizard-toggle-text">
          <strong>Use Mock Mode for now</strong>
          <small>No model calls. Demo task runs with fixtures.</small>
        </span>
      </label>

      <fieldset class="wizard-fieldset" disabled={mockMode}>
        <label class="wizard-field">
          <span>
            Anthropic API key <small class="wizard-field-hint">(starts with sk-ant-…)</small>
          </span>
          <input
            type="password"
            value={llmAnthropic}
            onInput={(e) =>
              onChange({ llmAnthropic: (e.target as HTMLInputElement).value })
            }
            placeholder="sk-ant-..."
            autoComplete="off"
          />
        </label>
        <label class="wizard-field">
          <span>
            OpenAI API key <small class="wizard-field-hint">(starts with sk-…)</small>
          </span>
          <input
            type="password"
            value={llmOpenai}
            onInput={(e) =>
              onChange({ llmOpenai: (e.target as HTMLInputElement).value })
            }
            placeholder="sk-..."
            autoComplete="off"
          />
        </label>
      </fieldset>

      <div class="wizard-actions">
        <button class="wizard-btn-secondary" onClick={onBack}>
          ← Back
        </button>
        <button class="wizard-btn-primary" onClick={onNext}>
          Continue →
        </button>
      </div>
    </section>
  );
}

function Step3({
  tenantName,
  mockMode,
  llmConfigured,
  submitting,
  error,
  remediation,
  onBack,
  onStart,
}: {
  tenantName: string;
  mockMode: boolean;
  llmConfigured: boolean;
  submitting: boolean;
  error: string | null;
  remediation: string | null;
  onBack: () => void;
  onStart: () => void;
}) {
  return (
    <section class="wizard-step">
      <p class="wizard-eyebrow">Step 3 of 3</p>
      <h1 class="wizard-title">Ready to go.</h1>
      <p class="wizard-lead">Quick recap, then a single click and you're in the dashboard.</p>

      <dl class="wizard-summary">
        <div>
          <dt>Workspace</dt>
          <dd>
            <code>{tenantName}</code>
          </dd>
        </div>
        <div>
          <dt>Mode</dt>
          <dd>
            {mockMode
              ? "Mock — deterministic fixtures, $0"
              : llmConfigured
                ? "Live — your LLM keys"
                : "Live — keys deferred (add later in Settings)"}
          </dd>
        </div>
      </dl>

      {error && (
        <div class="wizard-error">
          <strong>{error}</strong>
          {remediation && <p>{remediation}</p>}
        </div>
      )}

      <div class="wizard-actions">
        <button class="wizard-btn-secondary" onClick={onBack} disabled={submitting}>
          ← Back
        </button>
        <button class="wizard-btn-primary" onClick={onStart} disabled={submitting}>
          {submitting ? "Starting…" : "Start →"}
        </button>
      </div>
    </section>
  );
}

function Complete({ tenantName, apiKey }: { tenantName: string; apiKey: string }) {
  const [copied, setCopied] = useState(false);
  const onCopy = async () => {
    await navigator.clipboard.writeText(apiKey);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };
  return (
    <section class="wizard-step wizard-step-complete">
      <p class="wizard-eyebrow wizard-eyebrow-success">Workspace ready</p>
      <h1 class="wizard-title">Save this key.</h1>
      <p class="wizard-lead">
        You will not see it again. Stored encrypted in the dashboard config; copy it once and
        keep it somewhere safe.
      </p>

      <div class="wizard-apikey">
        <code>{apiKey}</code>
        <button onClick={onCopy} class="wizard-btn-secondary">
          {copied ? "Copied ✓" : "Copy"}
        </button>
      </div>

      <p class="wizard-checklist">
        Now click below to open the dashboard for <strong>{tenantName}</strong>. The first card
        you see is "Run sample task" — that produces the 71% reduction number on your own laptop
        in about a minute.
      </p>

      <a class="wizard-btn-primary wizard-btn-large" href={`/?ftux=run-sample&tenant=${encodeURIComponent(tenantName)}`}>
        Open dashboard →
      </a>
    </section>
  );
}

function Logo() {
  return (
    <a href="/" aria-label="Plinth home" class="wizard-logo">
      <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">
        <defs>
          <linearGradient id="wizardRock" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stop-color="#ff8551" />
            <stop offset="50%" stop-color="#d946ef" />
            <stop offset="100%" stop-color="#6366f1" />
          </linearGradient>
        </defs>
        <path
          d="M5 17 L8 9 L12 6 L17 6 L20 11 L20 17 Z"
          fill="url(#wizardRock)"
          stroke="#1d1f29"
          stroke-width="0.6"
          stroke-linejoin="round"
        />
      </svg>
      <span>Plinth</span>
    </a>
  );
}
