// SPDX-License-Identifier: Apache-2.0
// Visual Policy Editor for the Plynf proxy.
//
// Talks to:
//   GET    /proxy/v1/policies/effective
//   PUT    /proxy/v1/policies/{connector}/{tool}/override
//   DELETE /proxy/v1/policies/{connector}/{tool}/override
//
// The dashboard is expected to proxy /proxy/* to services/proxy (so credentials
// stay on the server side). For local development with a direct proxy URL, set
// VITE_PLYNF_PROXY_URL to point at e.g. http://localhost:7430.
//
// UX: each tool gets a card with two field lanes — "allowed" and "blocked".
// Fields can be dragged between lanes; the lane sets become allow_fields /
// deny_fields. Number inputs cover cache_ttl and max_response_tokens. A toggle
// covers strip_metadata, block_write_actions. PII redaction is Pro-only and
// shown as a gated control with an upgrade hint.

import { useEffect, useMemo, useRef, useState } from "preact/hooks";

interface ToolPolicy {
  connector: string;
  tool: string;
  allow_fields?: string[];
  deny_fields?: string[];
  max_response_tokens?: number;
  strip_metadata: boolean;
  cache_ttl?: number;
  redact_pii?: { fields: string[]; mode: "hash" | "mask" | "remove" };
  block_write_actions: boolean;
  has_override: boolean;
}

interface EffectivePolicies {
  tenant_id: string;
  tools: ToolPolicy[];
}

interface TierInfo {
  tenant_id: string;
  tier: "free" | "pro" | "enterprise";
  tokens_used_this_month: number;
}

const PROXY = (window as unknown as { VITE_PLYNF_PROXY_URL?: string })
  .VITE_PLYNF_PROXY_URL ?? "/proxy";

async function getEffective(): Promise<EffectivePolicies> {
  const r = await fetch(`${PROXY}/v1/policies/effective`);
  if (!r.ok) throw new Error(`policies fetch failed: ${r.status}`);
  return r.json();
}

async function getTier(): Promise<TierInfo | null> {
  try {
    const r = await fetch(`${PROXY}/v1/tier`);
    if (!r.ok) return null;
    return r.json();
  } catch {
    return null;
  }
}

async function putOverride(
  connector: string,
  tool: string,
  override: Partial<ToolPolicy>,
): Promise<void> {
  const r = await fetch(
    `${PROXY}/v1/policies/${connector}/${tool}/override`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(override),
    },
  );
  if (!r.ok) {
    const body = await r.text();
    throw new Error(`save failed: ${r.status} ${body.slice(0, 200)}`);
  }
}

async function clearOverride(connector: string, tool: string): Promise<void> {
  const r = await fetch(`${PROXY}/v1/policies/${connector}/${tool}/override`, {
    method: "DELETE",
  });
  if (!r.ok) throw new Error(`reset failed: ${r.status}`);
}

export function PoliciesEditor() {
  const [data, setData] = useState<EffectivePolicies | null>(null);
  const [tier, setTier] = useState<TierInfo | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([getEffective(), getTier()])
      .then(([d, t]) => {
        setData(d);
        setTier(t);
      })
      .catch((e) => setError(String(e)));
  }, []);

  if (error) {
    return (
      <div class="container">
        <h1>Policy editor</h1>
        <p class="error">{error}</p>
      </div>
    );
  }
  if (!data) {
    return (
      <div class="container">
        <h1>Policy editor</h1>
        <p>Loading…</p>
      </div>
    );
  }

  // Group tools by connector for the sidebar.
  const grouped = useMemo(() => {
    const out: Record<string, ToolPolicy[]> = {};
    for (const t of data.tools) {
      (out[t.connector] ??= []).push(t);
    }
    return out;
  }, [data]);

  return (
    <div class="container policy-editor">
      <header class="policy-editor__header">
        <div>
          <h1>Policy editor</h1>
          <p class="muted">
            Tenant <code>{data.tenant_id}</code>
            {tier && (
              <>
                {" · "}
                Tier <strong>{tier.tier}</strong> ·{" "}
                {tier.tokens_used_this_month.toLocaleString()} tokens used
                this month
              </>
            )}
          </p>
        </div>
      </header>

      {Object.entries(grouped).map(([connector, tools]) => (
        <section class="policy-editor__connector" key={connector}>
          <h2>{connector}</h2>
          <div class="policy-editor__tool-grid">
            {tools.map((t) => (
              <ToolCard
                key={`${connector}/${t.tool}`}
                tool={t}
                tier={tier?.tier ?? "free"}
                onSaved={(next) =>
                  setData((d) =>
                    d
                      ? {
                          ...d,
                          tools: d.tools.map((x) =>
                            x.connector === connector && x.tool === t.tool
                              ? { ...next, has_override: true }
                              : x,
                          ),
                        }
                      : d,
                  )
                }
                onReset={() =>
                  // Force a refetch — easiest way to get the shipped default back.
                  getEffective().then(setData)
                }
              />
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}

interface ToolCardProps {
  tool: ToolPolicy;
  tier: "free" | "pro" | "enterprise";
  onSaved: (next: ToolPolicy) => void;
  onReset: () => void;
}

function ToolCard({ tool, tier, onSaved, onReset }: ToolCardProps) {
  // Local edit state — committed to the server via PUT on Save.
  const [allow, setAllow] = useState<string[]>(tool.allow_fields ?? []);
  const [deny, setDeny] = useState<string[]>(tool.deny_fields ?? []);
  const [maxTokens, setMaxTokens] = useState<number | "">(
    tool.max_response_tokens ?? "",
  );
  const [cacheTtl, setCacheTtl] = useState<number | "">(tool.cache_ttl ?? "");
  const [stripMeta, setStripMeta] = useState(tool.strip_metadata);
  const [blockWrites, setBlockWrites] = useState(tool.block_write_actions);
  const [newFieldInput, setNewFieldInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const dirty =
    !arrayEqual(allow, tool.allow_fields ?? []) ||
    !arrayEqual(deny, tool.deny_fields ?? []) ||
    (maxTokens || null) !== (tool.max_response_tokens ?? null) ||
    (cacheTtl || null) !== (tool.cache_ttl ?? null) ||
    stripMeta !== tool.strip_metadata ||
    blockWrites !== tool.block_write_actions;

  async function save() {
    setBusy(true);
    setMsg(null);
    try {
      const body: Partial<ToolPolicy> = {
        allow_fields: allow,
        deny_fields: deny,
        strip_metadata: stripMeta,
        block_write_actions: blockWrites,
      };
      if (maxTokens !== "") body.max_response_tokens = Number(maxTokens);
      if (cacheTtl !== "") body.cache_ttl = Number(cacheTtl);
      await putOverride(tool.connector, tool.tool, body);
      setMsg("Saved.");
      onSaved({
        ...tool,
        allow_fields: allow,
        deny_fields: deny,
        max_response_tokens:
          maxTokens === "" ? undefined : Number(maxTokens),
        cache_ttl: cacheTtl === "" ? undefined : Number(cacheTtl),
        strip_metadata: stripMeta,
        block_write_actions: blockWrites,
        has_override: true,
      });
    } catch (e) {
      setMsg(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function reset() {
    setBusy(true);
    setMsg(null);
    try {
      await clearOverride(tool.connector, tool.tool);
      setMsg("Reset to default.");
      onReset();
    } catch (e) {
      setMsg(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div class="policy-card">
      <header>
        <h3>
          <code>{tool.tool}</code>
        </h3>
        {tool.has_override && <span class="badge badge--override">Overridden</span>}
      </header>

      <FieldLanes
        allow={allow}
        deny={deny}
        onChange={(a, d) => {
          setAllow(a);
          setDeny(d);
        }}
      />

      <div class="policy-card__add">
        <input
          type="text"
          placeholder="Add another field name…"
          value={newFieldInput}
          onInput={(e) =>
            setNewFieldInput((e.currentTarget as HTMLInputElement).value)
          }
        />
        <button
          type="button"
          onClick={() => {
            const v = newFieldInput.trim();
            if (!v || allow.includes(v) || deny.includes(v)) return;
            setAllow((a) => [...a, v]);
            setNewFieldInput("");
          }}
        >
          + Add to allow
        </button>
      </div>

      <div class="policy-card__numbers">
        <label>
          Cache TTL (s)
          <input
            type="number"
            value={cacheTtl}
            min={0}
            onInput={(e) => {
              const v = (e.currentTarget as HTMLInputElement).value;
              setCacheTtl(v === "" ? "" : Number(v));
            }}
          />
        </label>
        <label>
          Max response tokens
          <input
            type="number"
            value={maxTokens}
            min={0}
            onInput={(e) => {
              const v = (e.currentTarget as HTMLInputElement).value;
              setMaxTokens(v === "" ? "" : Number(v));
            }}
          />
        </label>
      </div>

      <div class="policy-card__toggles">
        <label>
          <input
            type="checkbox"
            checked={stripMeta}
            onChange={(e) => setStripMeta((e.target as HTMLInputElement).checked)}
          />
          Strip metadata fields (audit timestamps, internal ids…)
        </label>
        <label>
          <input
            type="checkbox"
            checked={blockWrites}
            onChange={(e) =>
              setBlockWrites((e.target as HTMLInputElement).checked)
            }
          />
          Block write actions (create_*, update_*, delete_*)
        </label>
        <label class={tier === "free" ? "policy-card__pro-gate" : ""}>
          <input
            type="checkbox"
            disabled={tier === "free"}
            checked={Boolean(tool.redact_pii)}
            readonly
          />
          Redact PII (hash sensitive fields){" "}
          {tier === "free" && (
            <span class="badge badge--pro">Pro</span>
          )}
        </label>
      </div>

      <footer>
        <button
          type="button"
          disabled={!dirty || busy}
          onClick={save}
          class="btn btn-primary"
        >
          {busy ? "Saving…" : "Save"}
        </button>
        {tool.has_override && (
          <button
            type="button"
            disabled={busy}
            onClick={reset}
            class="btn btn-secondary"
          >
            Reset to default
          </button>
        )}
        {msg && <span class="muted">{msg}</span>}
      </footer>
    </div>
  );
}

interface FieldLanesProps {
  allow: string[];
  deny: string[];
  onChange: (allow: string[], deny: string[]) => void;
}

function FieldLanes({ allow, deny, onChange }: FieldLanesProps) {
  // Drag-and-drop between lanes. The dragged field name is stored in
  // dataTransfer; the drop target decides which lane it lands in.
  const dragField = useRef<string | null>(null);

  function handleDragStart(field: string) {
    dragField.current = field;
  }

  function handleDrop(target: "allow" | "deny") {
    const f = dragField.current;
    dragField.current = null;
    if (!f) return;
    const nextAllow = allow.filter((x) => x !== f);
    const nextDeny = deny.filter((x) => x !== f);
    if (target === "allow") nextAllow.push(f);
    else nextDeny.push(f);
    onChange(nextAllow, nextDeny);
  }

  function remove(field: string) {
    onChange(allow.filter((x) => x !== field), deny.filter((x) => x !== field));
  }

  return (
    <div class="lanes">
      <Lane
        label="Allowed"
        accent="ok"
        fields={allow}
        onDragStart={handleDragStart}
        onDrop={() => handleDrop("allow")}
        onRemove={remove}
      />
      <Lane
        label="Blocked"
        accent="warn"
        fields={deny}
        onDragStart={handleDragStart}
        onDrop={() => handleDrop("deny")}
        onRemove={remove}
      />
    </div>
  );
}

interface LaneProps {
  label: string;
  accent: "ok" | "warn";
  fields: string[];
  onDragStart: (f: string) => void;
  onDrop: () => void;
  onRemove: (f: string) => void;
}

function Lane({ label, accent, fields, onDragStart, onDrop, onRemove }: LaneProps) {
  return (
    <div
      class={`lane lane--${accent}`}
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => {
        e.preventDefault();
        onDrop();
      }}
    >
      <header>{label}</header>
      <ul>
        {fields.length === 0 && <li class="lane__empty">Drop fields here</li>}
        {fields.map((f) => (
          <li
            key={f}
            draggable
            onDragStart={() => onDragStart(f)}
            class="chip"
          >
            <span>{f}</span>
            <button
              type="button"
              class="chip__x"
              onClick={() => onRemove(f)}
              aria-label={`Remove ${f}`}
            >
              ×
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

function arrayEqual(a: string[], b: string[]): boolean {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}
