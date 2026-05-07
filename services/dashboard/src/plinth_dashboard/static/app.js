// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors
//
// Plinth Dashboard SPA — vanilla JS, no build step.
// Polls /api/overview every 5s on the overview page; renders a workspace
// detail page on hash route #/workspaces/<id>.

(() => {
  "use strict";

  const REFRESH_MS = 5000;
  const intFmt = new Intl.NumberFormat("en-US");
  let pollTimer = null;
  let labelTimer = null;
  let lastFetchAt = null;
  let autoRefresh = true;
  let currentRoute = { name: "overview", params: {} };

  // ---- formatting helpers ----------------------------------------------

  function fmtUSD(value) {
    const n = Number(value || 0);
    if (n === 0) return "$0.0000";
    if (n >= 1) return "$" + n.toFixed(2);
    return "$" + n.toFixed(4);
  }

  function fmtBytes(value) {
    const n = Number(value || 0);
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / (1024 * 1024)).toFixed(2) + " MB";
  }

  function fmtMs(value) {
    const n = Number(value || 0);
    if (n < 1000) return n + "ms";
    return (n / 1000).toFixed(2) + "s";
  }

  function fmtTime(ts) {
    if (!ts) return "—";
    const d = new Date(ts);
    if (Number.isNaN(d.getTime())) return "—";
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    const ss = String(d.getSeconds()).padStart(2, "0");
    return `${hh}:${mm}:${ss}`;
  }

  function fmtDate(ts) {
    if (!ts) return "—";
    const d = new Date(ts);
    if (Number.isNaN(d.getTime())) return "—";
    return d.toLocaleString();
  }

  function shortId(id, head = 8) {
    if (!id) return "—";
    const s = String(id);
    if (s.length <= head + 3) return s;
    return s.slice(0, head) + "…";
  }

  function safe(value, fallback) {
    return value === null || value === undefined || value === "" ? fallback : value;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      "\"": "&quot;",
      "'": "&#39;",
    }[c]));
  }

  // ---- DOM helpers ------------------------------------------------------

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  function setSpinner(active) {
    const spin = $("#spinner");
    const label = $("#refresh-label");
    if (!spin || !label) return;
    spin.hidden = !active;
    label.textContent = active ? "Refreshing" : "Refresh";
  }

  function mountTemplate(id) {
    const tpl = document.getElementById(id);
    const root = $("#view-root");
    root.innerHTML = "";
    if (!tpl) return root;
    root.appendChild(tpl.content.cloneNode(true));
    return root;
  }

  // ---- API --------------------------------------------------------------

  async function api(path) {
    const r = await fetch(path, { headers: { Accept: "application/json" } });
    if (!r.ok) {
      let msg = "HTTP " + r.status;
      try {
        const body = await r.json();
        if (body && body.error && body.error.message) msg = body.error.message;
      } catch (_) { /* ignore */ }
      throw new Error(msg);
    }
    return r.json();
  }

  // ---- router -----------------------------------------------------------

  function parseRoute() {
    const hash = (location.hash || "").replace(/^#\/?/, "");
    if (!hash || hash === "" || hash === "/") {
      return { name: "overview", params: {} };
    }
    const parts = hash.split("/").filter(Boolean);
    if (parts[0] === "workspaces" && parts[1]) {
      return { name: "workspace", params: { ws_id: parts[1] } };
    }
    return { name: "overview", params: {} };
  }

  function renderCrumb(route) {
    const crumb = $("#crumb");
    if (!crumb) return;
    if (route.name === "workspace") {
      crumb.textContent = "/ workspaces / " + route.params.ws_id;
    } else {
      crumb.textContent = "";
    }
  }

  function navigate() {
    const route = parseRoute();
    currentRoute = route;
    renderCrumb(route);
    if (route.name === "workspace") {
      mountTemplate("tpl-workspace");
      stopPolling();
      void loadWorkspace(route.params.ws_id);
    } else {
      mountTemplate("tpl-overview");
      void refresh();
      startPolling();
    }
  }

  // ---- overview rendering ----------------------------------------------

  function renderTiles(data) {
    const tilesEl = $("#tiles");
    if (!tilesEl) return;
    const tools = (data.tools || {}).count || 0;
    const ws = (data.workspaces || {}).count || 0;
    const audit = data.audit || {};
    const calls = audit.total_invocations || 0;
    const cost = audit.total_cost_usd || 0;
    const cached = audit.cached_count || 0;
    const hitRate = calls > 0 ? (cached / calls) * 100 : 0;

    const tiles = [
      {
        label: "Workspaces",
        value: intFmt.format(ws),
      },
      {
        label: "Tools",
        value: intFmt.format(tools),
      },
      {
        label: "Tool calls",
        value: intFmt.format(calls),
        sub: `${intFmt.format(cached)} cached · ${hitRate.toFixed(0)}% hit-rate`,
      },
      {
        label: "Cost (24h)",
        value: fmtUSD(cost),
        sub: audit.error_count
          ? `${audit.error_count} errors`
          : "no errors",
      },
    ];

    tilesEl.innerHTML = tiles.map((t) => `
      <div class="tile">
        <span class="label">${escapeHtml(t.label)}</span>
        <span class="value">${escapeHtml(t.value)}</span>
        ${t.sub ? `<span class="sub">${escapeHtml(t.sub)}</span>` : ""}
      </div>
    `).join("");
  }

  function renderServices(data) {
    const root = $("#services");
    if (!root) return;
    const services = data.services || {};
    const order = [
      ["workspace", "Workspace"],
      ["gateway", "Gateway"],
      ["mock_mcp", "Mock MCP"],
      ["identity", "Identity"],
    ];
    root.innerHTML = order.map(([key, label]) => {
      const s = services[key] || {};
      const ok = s.status === "up";
      const cls = ok ? "ok" : "err";
      const glyph = ok ? "✓" : "✕";
      const ver = s.version ? `v${s.version}` : "";
      const url = s.url || "";
      return `
        <li class="service-row">
          <span class="check ${cls}" title="${ok ? "healthy" : "unreachable"}">${glyph}</span>
          <span class="name">${escapeHtml(label)}</span>
          <span class="url">${escapeHtml(url)}</span>
          <span class="ver">${escapeHtml(ver)}</span>
        </li>
      `;
    }).join("");
  }

  function renderWorkspacesTable(data) {
    const body = $("#workspaces-body");
    const countEl = $("#workspaces-count");
    if (!body) return;
    const rows = (data.workspaces || {}).list || [];
    if (countEl) countEl.textContent = rows.length ? `${rows.length} total` : "";
    if (!rows.length) {
      body.innerHTML = `<tr class="empty"><td colspan="5">No workspaces yet.</td></tr>`;
      return;
    }
    body.innerHTML = rows.map((w) => `
      <tr>
        <td class="id" title="${escapeHtml(w.id || "")}">${escapeHtml(shortId(w.id, 14))}</td>
        <td>${escapeHtml(safe(w.name, w.id || "—"))}</td>
        <td>${escapeHtml(safe(w.tenant_id, "default"))}</td>
        <td class="time">${escapeHtml(fmtDate(w.created_at))}</td>
        <td class="actions">
          <a class="btn small" href="#/workspaces/${encodeURIComponent(w.id)}">view</a>
        </td>
      </tr>
    `).join("");
  }

  function renderTenantsTable(data) {
    const body = $("#tenants-body");
    const countEl = $("#tenants-count");
    if (!body) return;
    const rows = (data.tenants || {}).list || [];
    if (countEl) countEl.textContent = rows.length ? `${rows.length} total` : "";
    if (!rows.length) {
      body.innerHTML = `<tr class="empty"><td colspan="4">No tenants yet.</td></tr>`;
      return;
    }
    body.innerHTML = rows.map((t) => `
      <tr>
        <td>${escapeHtml(safe(t.id, "default"))}</td>
        <td class="num">${intFmt.format(Number(t.workspace_count || 0))}</td>
        <td class="num">${intFmt.format(Number(t.tool_count || 0))}</td>
        <td class="num">${intFmt.format(Number(t.audit_count || 0))}</td>
      </tr>
    `).join("");
  }

  function renderToolCalls(events) {
    const body = $("#audit-body");
    if (!body) return;
    const rows = (events || []).slice(0, 50);
    if (!rows.length) {
      body.innerHTML = `<tr class="empty"><td colspan="6">No tool calls yet.</td></tr>`;
      return;
    }
    body.innerHTML = rows.map((e) => {
      const cachedTag = e.error
        ? `<span class="tag err" title="${escapeHtml(e.error)}">err</span>`
        : (e.cached
          ? `<span class="tag cached">yes</span>`
          : `<span class="tag fresh">no</span>`);
      return `
        <tr>
          <td class="time">${escapeHtml(fmtTime(e.timestamp))}</td>
          <td class="tool">${escapeHtml(e.tool_id || "?")}</td>
          <td>${cachedTag}</td>
          <td class="num">${escapeHtml(fmtMs(e.duration_ms))}</td>
          <td class="num">${escapeHtml(fmtUSD(e.cost_estimate_usd))}</td>
          <td class="id" title="${escapeHtml(e.id || "")}">${escapeHtml(shortId(e.id, 10))}</td>
        </tr>
      `;
    }).join("");
  }

  function renderOtlpStatus(observability) {
    const root = $("#otlp-body");
    const pill = $("#otlp-status-pill");
    if (!root) return;
    const obs = observability || {};
    const enabled = !!obs.otlp_enabled;
    const endpoint = obs.otlp_endpoint || "—";
    const emitted = Number(obs.events_emitted || 0);
    const errors = Number(obs.flush_errors || 0);
    const lastEmit = obs.last_emit_at;
    const events5min = Number(obs.events_emitted_5min || 0);
    const errors5min = Number(obs.errors_5min || 0);

    if (pill) {
      const cls = enabled ? "ok" : "off";
      const text = enabled ? "enabled" : "disabled";
      pill.innerHTML = `<span class="tag ${cls}">${escapeHtml(text)}</span>`;
    }

    const rows = [
      ["Status", enabled ? "enabled" : "disabled"],
      ["Endpoint", enabled ? endpoint : "—"],
      ["Events emitted", intFmt.format(emitted)],
      ["Last emit", enabled ? fmtDate(lastEmit) : "—"],
      ["Flush errors", intFmt.format(errors)],
      ["Events (5 min)", intFmt.format(events5min)],
      ["Errors (5 min)", intFmt.format(errors5min)],
    ];

    let html = rows
      .map(
        ([k, v]) => `
          <span class="otlp-key">${escapeHtml(k)}</span>
          <span class="otlp-val">${escapeHtml(String(v))}</span>
        `
      )
      .join("");

    if (!enabled) {
      html += `
        <p class="otlp-hint">
          Set <code>PLINTH_OTLP_ENABLED=true</code> and
          <code>PLINTH_OTLP_ENDPOINT=&lt;collector-url&gt;</code> on the
          gateway to enable the OTLP/HTTP event stream.
        </p>
      `;
    }
    root.innerHTML = html;
  }

  function renderDeadLetters(entries) {
    const card = $("#deadletters-card");
    const body = $("#deadletters-body");
    const countEl = $("#deadletters-count");
    if (!card || !body) return;

    const rows = (entries || []).slice(0, 10);
    if (!rows.length) {
      // Hide the panel completely when there's nothing to show — avoids
      // permanent visual noise for the common case.
      card.hidden = true;
      if (countEl) countEl.textContent = "";
      return;
    }
    card.hidden = false;
    const totalCount = rows.reduce(
      (acc, r) => acc + Number(r.deadletter_count || 0),
      0,
    );
    if (countEl) {
      countEl.textContent =
        rows.length === 1
          ? `${intFmt.format(totalCount)} message`
          : `${intFmt.format(rows.length)} channels · ${intFmt.format(totalCount)} messages`;
    }
    body.innerHTML = rows
      .map((r) => {
        const ws = r.workspace_id || "—";
        const channel = r.channel || "—";
        const count = Number(r.deadletter_count || 0);
        return `
        <tr>
          <td class="id" title="${escapeHtml(ws)}">${escapeHtml(shortId(ws, 14))}</td>
          <td class="mono">${escapeHtml(channel)}</td>
          <td class="num">${escapeHtml(intFmt.format(count))}</td>
          <td class="actions">
            <button class="btn small dlq-inspect"
                    type="button"
                    data-ws="${escapeHtml(ws)}"
                    data-channel="${escapeHtml(channel)}">inspect</button>
          </td>
        </tr>
      `;
      })
      .join("");
  }

  // Inspect a single channel's DLQ in a modal.
  async function openDlqModal(wsId, channel) {
    const modal = $("#dlq-modal");
    const titleEl = $("#dlq-modal-title");
    const bodyEl = $("#dlq-modal-body");
    if (!modal || !bodyEl) return;
    if (titleEl) {
      titleEl.textContent = `Dead letters · ${channel}`;
    }
    modal.hidden = false;
    bodyEl.innerHTML = `<p class="empty muted">loading&hellip;</p>`;
    try {
      const data = await api(
        `/api/workspaces/${encodeURIComponent(wsId)}/channels/${encodeURIComponent(
          channel,
        )}/deadletter?limit=20`,
      );
      const msgs = data.messages || [];
      if (!msgs.length) {
        bodyEl.innerHTML = `<p class="empty muted">No dead letters in this channel.</p>`;
        return;
      }
      bodyEl.innerHTML = msgs
        .map((m) => {
          const errors = parseValidationErrors(m.headers || {});
          const errorList = errors
            .map(
              (e) =>
                `<li class="dlq-err">
                   <code>${escapeHtml(e.path || "/")}</code>
                   <span>${escapeHtml(e.message || "")}</span>
                 </li>`,
            )
            .join("");
          return `
            <div class="dlq-item">
              <div class="dlq-item-head">
                <span class="mono">${escapeHtml(m.id || "—")}</span>
                <span class="muted">seq ${escapeHtml(String(m.seq || "—"))} · ${escapeHtml(
            fmtDate(m.sent_at),
          )}</span>
              </div>
              <pre class="dlq-payload">${escapeHtml(
                JSON.stringify(m.payload, null, 2),
              )}</pre>
              ${errorList ? `<ul class="dlq-errs">${errorList}</ul>` : ""}
            </div>
          `;
        })
        .join("");
    } catch (err) {
      bodyEl.innerHTML = `<p class="empty err">failed: ${escapeHtml(err.message)}</p>`;
    }
  }

  function parseValidationErrors(headers) {
    const raw = headers["x-validation-errors"];
    if (!raw) return [];
    try {
      const arr = JSON.parse(raw);
      if (!Array.isArray(arr)) return [];
      return arr.map((e) => ({
        path: Array.isArray(e.path) ? e.path.join(".") : "",
        message: e.message || "",
      }));
    } catch (_) {
      return [];
    }
  }

  function closeDlqModal() {
    const modal = $("#dlq-modal");
    if (modal) modal.hidden = true;
  }

  function renderTimeSeries(timeseries) {
    const canvas = document.getElementById("timeseries-canvas");
    const summary = $("#timeseries-summary");
    if (!canvas || !canvas.getContext) return;

    const buckets = (timeseries && timeseries.tool_calls_per_minute) || [];
    const counts = buckets.map((b) => Number(b.count || 0));
    const total = counts.reduce((a, b) => a + b, 0);
    const max = counts.length ? Math.max(...counts) : 0;
    const min = counts.length ? Math.min(...counts) : 0;
    if (summary) {
      summary.textContent = counts.length
        ? `${intFmt.format(total)} calls · max ${intFmt.format(
            max
          )}/min · min ${intFmt.format(min)}/min`
        : "no data";
    }

    const ctx = canvas.getContext("2d");
    const w = canvas.width;
    const h = canvas.height;
    ctx.clearRect(0, 0, w, h);

    // Padding for axis labels.
    const padL = 36;
    const padR = 8;
    const padT = 12;
    const padB = 22;
    const innerW = w - padL - padR;
    const innerH = h - padT - padB;

    // Background grid.
    ctx.strokeStyle = "#e6e6ea";
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let i = 0; i <= 4; i++) {
      const y = padT + (innerH / 4) * i;
      ctx.moveTo(padL, y);
      ctx.lineTo(w - padR, y);
    }
    ctx.stroke();

    // Axis labels (max + 0).
    ctx.fillStyle = "#9a9aa3";
    ctx.font = "11px ui-monospace, SFMono-Regular, monospace";
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    const axisMax = Math.max(max, 1);
    ctx.fillText(String(axisMax), padL - 6, padT);
    ctx.fillText("0", padL - 6, padT + innerH);

    // X-axis labels: -60 min · -30 min · now
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    ctx.fillText("−60m", padL, h - padB + 4);
    ctx.fillText("−30m", padL + innerW / 2, h - padB + 4);
    ctx.fillText("now", w - padR, h - padB + 4);

    if (!counts.length) {
      // Nothing else to draw without data.
      return;
    }

    // Filled-line sparkline.
    const stepX = innerW / Math.max(1, counts.length - 1);
    const norm = (v) => padT + innerH - (v / axisMax) * innerH;

    ctx.beginPath();
    counts.forEach((v, i) => {
      const x = padL + i * stepX;
      const y = norm(v);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    // Down to baseline + close to fill.
    ctx.lineTo(padL + (counts.length - 1) * stepX, padT + innerH);
    ctx.lineTo(padL, padT + innerH);
    ctx.closePath();
    ctx.fillStyle = "rgba(91, 108, 255, 0.12)";
    ctx.fill();

    // Stroke on top.
    ctx.beginPath();
    counts.forEach((v, i) => {
      const x = padL + i * stepX;
      const y = norm(v);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = "#5b6cff";
    ctx.lineWidth = 2;
    ctx.stroke();

    // Last-point dot.
    const lastIdx = counts.length - 1;
    ctx.fillStyle = "#3b4ce0";
    ctx.beginPath();
    ctx.arc(padL + lastIdx * stepX, norm(counts[lastIdx]), 3, 0, Math.PI * 2);
    ctx.fill();
  }

  function renderCostBars(byTool) {
    const root = $("#bars");
    if (!root) return;
    const rows = (byTool || []).slice().sort(
      (a, b) => Number(b.cost || b.cost_usd || 0) - Number(a.cost || a.cost_usd || 0)
    );
    if (!rows.length) {
      root.innerHTML = `<p class="empty muted">No cost data yet.</p>`;
      return;
    }
    const max = Math.max(
      ...rows.map((r) => Number(r.cost || r.cost_usd || 0)),
      0.000001,
    );
    root.innerHTML = rows.map((r) => {
      const cost = Number(r.cost || r.cost_usd || 0);
      const pct = Math.max(2, Math.round((cost / max) * 100));
      const calls = r.count != null ? `${intFmt.format(r.count)} calls · ` : "";
      return `
        <div class="bar-row">
          <span class="bar-label" title="${escapeHtml(r.tool_id || "")}">${escapeHtml(r.tool_id || "—")}</span>
          <span class="bar-track"><span class="bar-fill" style="width:${pct}%"></span></span>
          <span class="bar-value" title="${calls}${cost.toFixed(6)} USD">${escapeHtml(fmtUSD(cost))}</span>
        </div>
      `;
    }).join("");
  }

  // ---- main loop --------------------------------------------------------

  async function refresh() {
    if (currentRoute.name !== "overview") return;
    setSpinner(true);
    try {
      const overview = await api("/api/overview");
      renderTiles(overview);
      renderServices(overview);
      renderTenantsTable(overview);
      renderWorkspacesTable(overview);
      renderDeadLetters(overview.deadletters);
      renderOtlpStatus(overview.observability);
      renderTimeSeries(overview.timeseries);
      renderCostBars(overview.audit ? overview.audit.by_tool : []);

      // Tool calls list comes from the audit endpoint directly so we always
      // surface raw recent events with audit IDs etc.
      try {
        const audit = await api("/api/audit?limit=50");
        renderToolCalls(audit.events || []);
      } catch (e) {
        renderToolCalls([]);
      }

      lastFetchAt = Date.now();
      tickRefreshLabel();
    } catch (err) {
      const meta = $("#last-refresh");
      if (meta) meta.textContent = "fetch failed: " + err.message;
    } finally {
      setSpinner(false);
    }
  }

  function tickRefreshLabel() {
    const el = $("#last-refresh");
    if (!el || !lastFetchAt) return;
    const secs = Math.max(0, Math.round((Date.now() - lastFetchAt) / 1000));
    el.textContent = "last refresh: " + secs + "s ago";
  }

  function startPolling() {
    stopPolling();
    if (autoRefresh) {
      pollTimer = setInterval(() => { void refresh(); }, REFRESH_MS);
    }
    if (!labelTimer) labelTimer = setInterval(tickRefreshLabel, 1000);
  }

  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  // ---- workspace detail rendering --------------------------------------

  async function loadWorkspace(wsId) {
    const headEl = $("#ws-head");
    if (headEl) headEl.classList.add("loading");

    // Fetch all workspace facets in parallel; tolerate partial failures.
    const fetchOpt = (path) =>
      api(path).catch((err) => ({ __error: err.message }));

    const [meta, kv, snaps, channels, workflows] = await Promise.all([
      fetchOpt(`/api/workspaces/${encodeURIComponent(wsId)}`),
      fetchOpt(`/api/workspaces/${encodeURIComponent(wsId)}/kv`),
      fetchOpt(`/api/workspaces/${encodeURIComponent(wsId)}/snapshots`),
      fetchOpt(`/api/workspaces/${encodeURIComponent(wsId)}/channels`),
      fetchOpt(`/api/workspaces/${encodeURIComponent(wsId)}/workflows`),
    ]);

    renderWorkspaceHead(wsId, meta);
    renderKV(kv);
    renderSnapshots(snaps);
    renderChannels(channels);
    renderWorkflows(workflows);

    lastFetchAt = Date.now();
    tickRefreshLabel();
  }

  function renderWorkspaceHead(wsId, meta) {
    const nameEl = $("#ws-name");
    const idEl = $("#ws-id");
    const metaEl = $("#ws-meta");
    if (nameEl) nameEl.textContent = (meta && !meta.__error) ? (meta.name || wsId) : wsId;
    if (idEl) idEl.textContent = wsId;
    if (metaEl) {
      if (!meta || meta.__error) {
        metaEl.innerHTML = `<span class="tag err" title="${escapeHtml(
          (meta && meta.__error) || "load failed"
        )}">unavailable</span>`;
        return;
      }
      metaEl.innerHTML = `
        <span>created ${escapeHtml(fmtDate(meta.created_at))}</span>
        <span>updated ${escapeHtml(fmtDate(meta.updated_at))}</span>
      `;
    }
  }

  // Generic table renderer shared by KV / snapshots / channels.
  // ``cfg`` carries: bodyId, countId, listKey, emptyText, label, rowFn(item).
  function renderTable(payload, cfg) {
    const body = $(cfg.bodyId);
    const count = $(cfg.countId);
    if (!body) return;
    const colspan = cfg.colspan || 3;
    if (!payload || payload.__error) {
      body.innerHTML = `<tr class="empty"><td colspan="${colspan}">${escapeHtml(
        payload && payload.__error ? payload.__error : "unavailable"
      )}</td></tr>`;
      if (count) count.textContent = "";
      return;
    }
    let rows = payload[cfg.listKey] || [];
    if (cfg.filter) rows = rows.filter(cfg.filter);
    if (count) count.textContent = rows.length ? `${rows.length} ${cfg.label}` : "";
    if (!rows.length) {
      body.innerHTML = `<tr class="empty"><td colspan="${colspan}">${escapeHtml(cfg.emptyText)}</td></tr>`;
      return;
    }
    body.innerHTML = rows.map(cfg.rowFn).join("");
  }

  function renderKV(kv) {
    renderTable(kv, {
      bodyId: "#kv-body", countId: "#kv-count", listKey: "entries",
      label: "keys", emptyText: "No keys.",
      filter: (e) => !e.deleted,
      rowFn: (e) => `<tr>
        <td class="mono">${escapeHtml(e.key || "")}</td>
        <td class="num">${escapeHtml(String(e.version != null ? e.version : "—"))}</td>
        <td class="time">${escapeHtml(fmtDate(e.created_at))}</td>
      </tr>`,
    });
  }

  function renderSnapshots(snaps) {
    renderTable(snaps, {
      bodyId: "#snap-body", countId: "#snap-count", listKey: "snapshots",
      label: "snapshots", emptyText: "No snapshots.",
      rowFn: (s) => `<tr>
        <td class="id" title="${escapeHtml(s.id || "")}">${escapeHtml(shortId(s.id, 12))}</td>
        <td>${escapeHtml(s.name || "—")}</td>
        <td class="time">${escapeHtml(fmtDate(s.created_at))}</td>
      </tr>`,
    });
  }

  function renderChannels(channels) {
    renderTable(channels, {
      bodyId: "#channel-body", countId: "#channel-count", listKey: "channels",
      label: "channels", emptyText: "No channels.",
      rowFn: (c) => `<tr>
        <td class="mono">${escapeHtml(c.name || "—")}</td>
        <td class="num">${escapeHtml(intFmt.format(c.message_count || 0))}</td>
        <td class="time">${escapeHtml(fmtDate(c.last_send_at || c.created_at))}</td>
      </tr>`,
    });
  }

  function renderWorkflows(workflows) {
    const root = $("#wf-body");
    const count = $("#wf-count");
    if (!root) return;
    if (!workflows || workflows.__error) {
      root.innerHTML = `<p class="empty muted">${escapeHtml(
        workflows && workflows.__error ? workflows.__error : "unavailable"
      )}</p>`;
      if (count) count.textContent = "";
      return;
    }
    const rows = workflows.workflows || [];
    if (count) count.textContent = rows.length ? `${rows.length} workflows` : "";
    if (!rows.length) {
      root.innerHTML = `<p class="empty muted">No workflows.</p>`;
      return;
    }
    root.innerHTML = rows.map((wf) => {
      const manifest = wf.steps_manifest || [];
      const steps = wf.steps || [];
      const stepByName = new Map();
      for (const s of steps) {
        if (!stepByName.has(s.name)) stepByName.set(s.name, s);
      }
      const total = manifest.length || steps.length;
      const done = steps.filter((s) => s.status === "completed").length;
      const pct = total > 0 ? Math.round((done / total) * 100) : 0;
      const status = wf.status || "pending";
      const stepRows = (manifest.length ? manifest : steps.map((s) => s.name)).map((name) => {
        const s = stepByName.get(name);
        const cls = s ? s.status : "pending";
        return `<li class="${escapeHtml(cls)}">${escapeHtml(name)}${
          s && s.status && s.status !== "pending"
            ? ` <span class="muted">— ${escapeHtml(s.status)}</span>`
            : ""
        }</li>`;
      }).join("");
      return `
        <div class="workflow">
          <div class="workflow-head">
            <span class="workflow-name">${escapeHtml(wf.name || wf.id || "workflow")}</span>
            <span class="workflow-status ${escapeHtml(status)}">${escapeHtml(status)}</span>
          </div>
          <div class="progress"><div class="bar" style="width:${pct}%"></div></div>
          <div class="workflow-meta">${done}/${total} steps · ${escapeHtml(wf.id || "")}</div>
          <ul class="steps-list">${stepRows}</ul>
        </div>
      `;
    }).join("");
  }

  // ---- bootstrap --------------------------------------------------------

  document.addEventListener("DOMContentLoaded", () => {
    const versionEl = $("#dashboard-version");
    if (versionEl) versionEl.textContent = "0.1.0";

    const refreshBtn = $("#refresh-btn");
    if (refreshBtn) refreshBtn.addEventListener("click", () => {
      if (currentRoute.name === "workspace") {
        void loadWorkspace(currentRoute.params.ws_id);
      } else {
        void refresh();
      }
    });

    const toggle = $("#autorefresh-toggle");
    if (toggle) {
      toggle.addEventListener("change", (e) => {
        autoRefresh = !!e.target.checked;
        if (currentRoute.name === "overview") startPolling();
      });
    }

    // Dead-letter modal: delegate clicks on the .dlq-inspect buttons.
    document.addEventListener("click", (event) => {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;
      const inspect = target.closest(".dlq-inspect");
      if (inspect instanceof HTMLElement) {
        event.preventDefault();
        const wsId = inspect.dataset.ws || "";
        const channel = inspect.dataset.channel || "";
        if (wsId && channel) void openDlqModal(wsId, channel);
        return;
      }
      // Click outside the card or on the close button → dismiss.
      if (
        target.id === "dlq-modal-close" ||
        target.id === "dlq-modal" ||
        target.classList.contains("dlq-modal")
      ) {
        closeDlqModal();
      }
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeDlqModal();
    });

    window.addEventListener("hashchange", navigate);
    navigate();
  });
})();
