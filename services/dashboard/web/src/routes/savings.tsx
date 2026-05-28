// SPDX-License-Identifier: Apache-2.0
// Savings dashboard for Plynf.
//
// Three blocks:
//   1. Headline cards — tokens saved, $ saved, savings %, cache hit rate.
//   2. Time-series sparkline — saved tokens per hour over the last 7 days.
//   3. Top connectors bar chart — which integrations drive the most savings.
//
// All charts are hand-rolled SVG (no chart library) to keep the bundle
// under 20 kB. Data source is the proxy at /proxy/v1/savings/{summary,timeseries}.

import { useEffect, useMemo, useState } from "preact/hooks";

const PROXY = (window as unknown as { VITE_PLYNF_PROXY_URL?: string })
  .VITE_PLYNF_PROXY_URL ?? "/proxy";

interface Summary {
  total_calls: number;
  total_raw_tokens: number;
  total_shaped_tokens: number;
  total_saved_tokens: number;
  savings_pct: number;
  total_cost_saved_usd: number;
  cache_hit_rate: number;
  top_connectors_by_savings: [string, number][];
}

interface TimeseriesPoint {
  ts: number;
  saved_tokens: number;
  shaped_tokens: number;
  raw_tokens: number;
  cost_saved_usd: number;
  calls: number;
}

interface TimeseriesResponse {
  bucket_s: number;
  points: TimeseriesPoint[];
}

export function SavingsDashboard() {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [series, setSeries] = useState<TimeseriesResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const refresh = () =>
      Promise.all([
        fetch(`${PROXY}/v1/savings/summary`).then((r) => r.json()),
        fetch(`${PROXY}/v1/savings/timeseries?bucket_s=3600&limit=168`).then((r) =>
          r.json(),
        ),
      ])
        .then(([s, ts]) => {
          setSummary(s);
          setSeries(ts);
          setError(null);
        })
        .catch((e) => setError(String(e)));

    refresh();
    const id = setInterval(refresh, 15_000);
    return () => clearInterval(id);
  }, []);

  if (error) {
    return (
      <div class="container">
        <h1>Savings</h1>
        <p class="error">{error}</p>
      </div>
    );
  }
  if (!summary) {
    return (
      <div class="container">
        <h1>Savings</h1>
        <p>Loading…</p>
      </div>
    );
  }

  return (
    <div class="container savings">
      <header class="savings__header">
        <h1>Savings</h1>
        <p class="muted">Auto-refresh every 15s · in-memory aggregate, will move to Postgres</p>
      </header>

      <div class="savings__cards">
        <Card
          label="Tokens saved"
          value={summary.total_saved_tokens.toLocaleString()}
          accent="ok"
        />
        <Card
          label="Cost saved"
          value={`$${summary.total_cost_saved_usd.toFixed(2)}`}
          accent="ok"
        />
        <Card
          label="Reduction"
          value={`${(summary.savings_pct * 100).toFixed(1)}%`}
          accent="ok"
        />
        <Card
          label="Cache hit rate"
          value={`${(summary.cache_hit_rate * 100).toFixed(0)}%`}
        />
        <Card
          label="Calls processed"
          value={summary.total_calls.toLocaleString()}
        />
        <Card
          label="Avg shaped / raw"
          value={
            summary.total_raw_tokens > 0
              ? `${summary.total_shaped_tokens.toLocaleString()} / ${summary.total_raw_tokens.toLocaleString()}`
              : "—"
          }
        />
      </div>

      <section class="savings__section">
        <h2>Saved tokens, last 7 days (hourly buckets)</h2>
        <Sparkline series={series} />
      </section>

      <section class="savings__section">
        <h2>Top connectors by savings</h2>
        <ConnectorBars rows={summary.top_connectors_by_savings} />
      </section>
    </div>
  );
}

function Card(props: { label: string; value: string; accent?: "ok" }) {
  return (
    <div class={`savings-card ${props.accent ? `savings-card--${props.accent}` : ""}`}>
      <div class="savings-card__label">{props.label}</div>
      <div class="savings-card__value">{props.value}</div>
    </div>
  );
}

interface SparklineProps {
  series: TimeseriesResponse | null;
}

function Sparkline({ series }: SparklineProps) {
  const points = series?.points ?? [];
  if (points.length === 0) {
    return <p class="muted">No tool calls yet. Run the demo to seed some data.</p>;
  }

  const width = 800;
  const height = 160;
  const padX = 8;
  const padY = 12;

  const max = useMemo(
    () => Math.max(1, ...points.map((p) => p.saved_tokens)),
    [points],
  );

  // Build a polyline path through the data and a filled area underneath.
  const xStep = (width - padX * 2) / Math.max(1, points.length - 1);
  const xs = points.map((_, i) => padX + i * xStep);
  const ys = points.map(
    (p) => height - padY - (p.saved_tokens / max) * (height - padY * 2),
  );
  const pathD = xs.map((x, i) => `${i === 0 ? "M" : "L"} ${x} ${ys[i]}`).join(" ");
  const areaD =
    `M ${xs[0]} ${height - padY} ` +
    xs.map((x, i) => `L ${x} ${ys[i]}`).join(" ") +
    ` L ${xs[xs.length - 1]} ${height - padY} Z`;

  return (
    <div class="savings__chart">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Saved tokens over time">
        <defs>
          <linearGradient id="sg" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#43c674" stop-opacity="0.4" />
            <stop offset="100%" stop-color="#43c674" stop-opacity="0" />
          </linearGradient>
        </defs>
        <path d={areaD} fill="url(#sg)" />
        <path d={pathD} fill="none" stroke="#43c674" stroke-width="1.5" />
      </svg>
      <div class="savings__chart-meta muted">
        Peak {max.toLocaleString()} tokens/hour · {points.length} buckets
      </div>
    </div>
  );
}

interface ConnectorBarsProps {
  rows: [string, number][];
}

function ConnectorBars({ rows }: ConnectorBarsProps) {
  if (rows.length === 0) {
    return <p class="muted">No connectors yet.</p>;
  }
  const max = Math.max(...rows.map((r) => r[1]));
  return (
    <ul class="savings-bars">
      {rows.map(([name, value]) => (
        <li key={name}>
          <div class="savings-bars__label">{name}</div>
          <div class="savings-bars__track">
            <div
              class="savings-bars__fill"
              style={`width: ${(value / max) * 100}%`}
            />
          </div>
          <div class="savings-bars__value">{value.toLocaleString()}</div>
        </li>
      ))}
    </ul>
  );
}
