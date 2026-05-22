// 3-step welcome wizard. From zero tenants to first running demo
// in three clicks per the open-tasks Block 4 acceptance criteria.
//
// Step 1: workspace name (autosuggested from OS user)
// Step 2: LLM key (Anthropic or OpenAI) OR "use mock mode for now"
// Step 3: summary + Generate API Key (shown once, copy-confirm required)
//
// On complete: POST /api/v1/bootstrap with the collected fields, then
// redirect to /?ftux=run-sample which the dashboard root reads and
// highlights the "Run sample task" card.

import { useState } from "preact/hooks";
import { api } from "@/lib/api";

type Step = 1 | 2 | 3 | 4;

interface State {
  workspaceName: string;
  llmProvider: "anthropic" | "openai" | "mock";
  llmKey: string;
  apiKey: string | null;
  copied: boolean;
  error: string | null;
  busy: boolean;
}

export function Welcome() {
  const [step, setStep] = useState<Step>(1);
  const [s, setS] = useState<State>({
    workspaceName: defaultWorkspaceName(),
    llmProvider: "mock",
    llmKey: "",
    apiKey: null,
    copied: false,
    error: null,
    busy: false,
  });

  async function finish() {
    setS({ ...s, busy: true, error: null });
    try {
      const r = await api.bootstrap({
        tenant_name: s.workspaceName,
        llm_keys: s.llmProvider === "mock"
          ? undefined
          : { [s.llmProvider]: s.llmKey },
        mock_mode: s.llmProvider === "mock",
      });
      setS({ ...s, busy: false, apiKey: r.api_key });
      setStep(4);
    } catch (e) {
      setS({ ...s, busy: false, error: (e as Error).message });
    }
  }

  return (
    <div class="wizard">
      <header class="wizard-header">
        <Brand />
        <Progress current={step} total={4} />
      </header>

      {step === 1 && (
        <StepCard title="What should we call this workspace?" hint="One workspace, many agents. You can add more later.">
          <input
            class="input"
            value={s.workspaceName}
            onInput={(e) => setS({ ...s, workspaceName: (e.target as HTMLInputElement).value })}
            placeholder="my-research"
            autoFocus
          />
          <Actions
            primary={{ label: "Next", onClick: () => setStep(2), disabled: !s.workspaceName.trim() }}
          />
        </StepCard>
      )}

      {step === 2 && (
        <StepCard title="LLM keys" hint="Bring your own. Or skip and use mock mode — no real LLM calls, but every other Plynf feature works.">
          <ChoiceRow
            choices={[
              { id: "mock",      label: "Use mock mode for now", sublabel: "Recommended for first-run" },
              { id: "anthropic", label: "Anthropic (Claude)",     sublabel: "I have an API key" },
              { id: "openai",    label: "OpenAI (GPT)",           sublabel: "I have an API key" },
            ]}
            value={s.llmProvider}
            onChange={(v) => setS({ ...s, llmProvider: v as State["llmProvider"], llmKey: "" })}
          />
          {s.llmProvider !== "mock" && (
            <input
              type="password"
              class="input"
              value={s.llmKey}
              onInput={(e) => setS({ ...s, llmKey: (e.target as HTMLInputElement).value })}
              placeholder={s.llmProvider === "anthropic" ? "sk-ant-..." : "sk-..."}
            />
          )}
          <Actions
            secondary={{ label: "Back", onClick: () => setStep(1) }}
            primary={{
              label: "Next",
              onClick: () => setStep(3),
              disabled: s.llmProvider !== "mock" && s.llmKey.length < 10,
            }}
          />
        </StepCard>
      )}

      {step === 3 && (
        <StepCard title="Confirm and generate" hint="One last thing: we'll create an API key. Save it now — Plynf never shows it again.">
          <dl class="summary">
            <dt>Workspace</dt><dd class="mono">{s.workspaceName}</dd>
            <dt>LLM mode</dt><dd>{s.llmProvider === "mock" ? "Mock (no real LLM)" : s.llmProvider}</dd>
          </dl>
          {s.error && <div class="error">{s.error}</div>}
          <Actions
            secondary={{ label: "Back", onClick: () => setStep(2), disabled: s.busy }}
            primary={{
              label: s.busy ? "Generating…" : "Generate API key",
              onClick: finish,
              disabled: s.busy,
            }}
          />
        </StepCard>
      )}

      {step === 4 && s.apiKey && (
        <StepCard title="Your API key" hint="This is the only time we show it. Save it somewhere safe — password manager, .env file, encrypted note.">
          <div class="api-key-display mono">
            {s.apiKey}
            <button
              class="copy-btn"
              onClick={() => {
                navigator.clipboard.writeText(s.apiKey!);
                setS({ ...s, copied: true });
                setTimeout(() => setS({ ...s, copied: false }), 1500);
              }}
            >
              {s.copied ? "Copied" : "Copy"}
            </button>
          </div>
          <label class="confirm">
            <input
              type="checkbox"
              onChange={(e) => {
                if ((e.target as HTMLInputElement).checked) {
                  window.location.href = "/?ftux=run-sample";
                }
              }}
            />
            I saved the key. Take me to the dashboard.
          </label>
        </StepCard>
      )}
    </div>
  );
}

// ─── Helpers + small components ───────────────────────────────────────

function defaultWorkspaceName(): string {
  return "my-workspace";
}

function Brand() {
  return (
    <div class="brand">
      <svg viewBox="0 0 24 24" width="24" height="24" aria-hidden="true">
        <defs>
          <linearGradient id="rockw" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stop-color="#ff8551"/>
            <stop offset="50%" stop-color="#d946ef"/>
            <stop offset="100%" stop-color="#6366f1"/>
          </linearGradient>
        </defs>
        <path d="M5 17 L8 9 L12 6 L17 6 L20 11 L20 17 Z" fill="url(#rockw)"/>
      </svg>
      <span>Plynf</span>
    </div>
  );
}

function Progress({ current, total }: { current: number; total: number }) {
  return (
    <div class="progress" role="progressbar" aria-valuenow={current} aria-valuemax={total}>
      {Array.from({ length: total }, (_, i) => (
        <span
          key={i}
          class={"dot " + (i < current ? "filled" : "")}
          aria-current={i === current - 1 ? "step" : undefined}
        />
      ))}
    </div>
  );
}

function StepCard(props: { title: string; hint: string; children: any }) {
  return (
    <main class="step-card">
      <h1 class="step-title">{props.title}</h1>
      <p class="step-hint">{props.hint}</p>
      <div class="step-body">{props.children}</div>
    </main>
  );
}

function ChoiceRow({ choices, value, onChange }: {
  choices: { id: string; label: string; sublabel?: string }[];
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div class="choice-row" role="radiogroup">
      {choices.map((c) => (
        <button
          key={c.id}
          class={"choice " + (value === c.id ? "selected" : "")}
          onClick={() => onChange(c.id)}
          role="radio"
          aria-checked={value === c.id}
        >
          <div class="label">{c.label}</div>
          {c.sublabel && <div class="sublabel">{c.sublabel}</div>}
        </button>
      ))}
    </div>
  );
}

function Actions(props: {
  primary: { label: string; onClick: () => void; disabled?: boolean };
  secondary?: { label: string; onClick: () => void; disabled?: boolean };
}) {
  return (
    <div class="actions">
      {props.secondary && (
        <button class="btn btn-secondary" onClick={props.secondary.onClick} disabled={props.secondary.disabled}>
          {props.secondary.label}
        </button>
      )}
      <button class="btn btn-primary" onClick={props.primary.onClick} disabled={props.primary.disabled}>
        {props.primary.label}
      </button>
    </div>
  );
}
