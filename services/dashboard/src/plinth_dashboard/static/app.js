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

  // ---- workflow view state ---------------------------------------------
  // Filters + sort persist across re-renders so a 5-second auto-refresh
  // doesn't clobber what the user picked.
  const wfListState = {
    rows: [],
    filterStatus: "",
    filterWorkspace: "",
    sortKey: "started",
    sortDir: "desc", // "asc" | "desc"
  };
  // Live cache of the most recent workflow detail payload + suppression
  // flag so auto-refresh pauses while the step modal is open.
  const wfDetailState = {
    wsId: null,
    wfId: null,
    workflow: null,
    modalOpen: false,
  };

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
    // ``location.hash`` carries both the path and any query-style suffix
    // (``#/workflows/wf_x?ws=ws_a``). We need to split the search portion
    // off ourselves because ``location.search`` only reflects the URL's
    // pre-hash query string.
    const raw = (location.hash || "").replace(/^#\/?/, "");
    const qIdx = raw.indexOf("?");
    const path = qIdx >= 0 ? raw.slice(0, qIdx) : raw;
    const query = qIdx >= 0 ? raw.slice(qIdx + 1) : "";
    const qs = new URLSearchParams(query);

    if (!path || path === "" || path === "/") {
      return { name: "overview", params: {} };
    }
    const parts = path.split("/").filter(Boolean);
    if (parts[0] === "workspaces" && parts[1]) {
      return { name: "workspace", params: { ws_id: parts[1] } };
    }
    if (parts[0] === "workflows") {
      if (parts[1]) {
        return {
          name: "workflow-detail",
          params: {
            wf_id: parts[1],
            ws_id: qs.get("ws") || "",
          },
        };
      }
      return { name: "workflows", params: {} };
    }
    return { name: "overview", params: {} };
  }

  function renderCrumb(route) {
    const crumb = $("#crumb");
    if (!crumb) return;
    if (route.name === "workspace") {
      crumb.textContent = "/ workspaces / " + route.params.ws_id;
    } else if (route.name === "workflows") {
      crumb.textContent = "/ workflows";
    } else if (route.name === "workflow-detail") {
      crumb.textContent =
        "/ workflows / " + (route.params.wf_id || "");
    } else {
      crumb.textContent = "";
    }
  }

  function renderTopnav(route) {
    // Highlight whichever top-level area the current route belongs to.
    const links = $$(".topnav-link");
    const active =
      route.name === "workflows" || route.name === "workflow-detail"
        ? "workflows"
        : "overview";
    links.forEach((el) => {
      const isActive = el.dataset.route === active;
      el.classList.toggle("active", isActive);
      if (isActive) {
        el.setAttribute("aria-current", "page");
      } else {
        el.removeAttribute("aria-current");
      }
    });
  }

  function navigate() {
    const route = parseRoute();
    currentRoute = route;
    renderCrumb(route);
    renderTopnav(route);
    closeWorkflowStepModal({ silent: true });
    if (route.name === "workspace") {
      mountTemplate("tpl-workspace");
      stopPolling();
      void loadWorkspace(route.params.ws_id);
    } else if (route.name === "workflows") {
      mountTemplate("tpl-workflows-list");
      wireWorkflowListControls();
      void refreshWorkflowsList();
      startPolling();
    } else if (route.name === "workflow-detail") {
      mountTemplate("tpl-workflow-detail");
      void refreshWorkflowDetail();
      startPolling();
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

  // Track which (workspace, channel) the DLQ modal is currently showing
  // so the new "Replay all" / "Purge older than 24h" buttons know what to
  // act on without trawling the DOM.
  const dlqModalState = { wsId: null, channel: null };

  // Inspect a single channel's DLQ in a modal.
  async function openDlqModal(wsId, channel) {
    const modal = $("#dlq-modal");
    const titleEl = $("#dlq-modal-title");
    const bodyEl = $("#dlq-modal-body");
    if (!modal || !bodyEl) return;
    if (titleEl) {
      titleEl.textContent = `Dead letters · ${channel}`;
    }
    dlqModalState.wsId = wsId;
    dlqModalState.channel = channel;
    setDlqModalStatus("");
    modal.hidden = false;
    await refreshDlqModalContents();
  }

  // Re-fetch the DLQ list and re-render the modal body. Pulled out of
  // ``openDlqModal`` so the "Replay all" / "Purge" buttons can invoke
  // it after they mutate state.
  async function refreshDlqModalContents() {
    const bodyEl = $("#dlq-modal-body");
    if (!bodyEl) return;
    const { wsId, channel } = dlqModalState;
    if (!wsId || !channel) return;
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

  // Show or hide the inline status banner above the DLQ list.
  function setDlqModalStatus(text, kind) {
    const el = $("#dlq-modal-status");
    if (!el) return;
    if (!text) {
      el.hidden = true;
      el.textContent = "";
      el.className = "dlq-modal-status";
      return;
    }
    el.hidden = false;
    el.textContent = text;
    el.className = "dlq-modal-status" + (kind ? " " + kind : "");
  }

  // Bulk replay every DLQ message in the open channel through the
  // currently attached schema (server caps the batch at 100). Successes
  // move to the main channel; failures stay in the DLQ.
  async function replayAllDeadletters() {
    const { wsId, channel } = dlqModalState;
    if (!wsId || !channel) return;
    const btn = $("#dlq-replay-all");
    if (btn) btn.disabled = true;
    setDlqModalStatus("replaying…", "info");
    try {
      const r = await fetch(
        `/api/workspaces/${encodeURIComponent(wsId)}/channels/${encodeURIComponent(
          channel,
        )}/deadletter/replay-all`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Accept: "application/json",
          },
          body: JSON.stringify({ max: 100 }),
        },
      );
      const body = await r.json().catch(() => ({}));
      if (!r.ok) {
        const msg = (body && body.error && body.error.message) || "HTTP " + r.status;
        setDlqModalStatus(`replay failed: ${msg}`, "err");
        return;
      }
      const succeeded = Number(body.succeeded || 0);
      const failed = Number(body.failed || 0);
      setDlqModalStatus(
        `replayed ${succeeded} · ${failed} still failing`,
        failed > 0 ? "warn" : "ok",
      );
      await refreshDlqModalContents();
    } catch (err) {
      setDlqModalStatus(`replay failed: ${err.message || err}`, "err");
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  // Permanently delete DLQ rows older than 24 hours. Destructive — the
  // operator must confirm via ``window.confirm`` before we make the call.
  async function purgeOldDeadletters() {
    const { wsId, channel } = dlqModalState;
    if (!wsId || !channel) return;
    const ok = window.confirm(
      `Permanently delete DLQ messages older than 24h on "${channel}"?\n\n` +
        `This cannot be undone.`,
    );
    if (!ok) return;
    const btn = $("#dlq-purge-old");
    if (btn) btn.disabled = true;
    setDlqModalStatus("purging…", "info");
    try {
      const r = await fetch(
        `/api/workspaces/${encodeURIComponent(wsId)}/channels/${encodeURIComponent(
          channel,
        )}/deadletter?older_than_seconds=86400`,
        {
          method: "DELETE",
          headers: { Accept: "application/json" },
        },
      );
      const body = await r.json().catch(() => ({}));
      if (!r.ok) {
        const msg = (body && body.error && body.error.message) || "HTTP " + r.status;
        setDlqModalStatus(`purge failed: ${msg}`, "err");
        return;
      }
      const purged = Number(body.purged || 0);
      setDlqModalStatus(`purged ${purged} message${purged === 1 ? "" : "s"}`, "ok");
      await refreshDlqModalContents();
    } catch (err) {
      setDlqModalStatus(`purge failed: ${err.message || err}`, "err");
    } finally {
      if (btn) btn.disabled = false;
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
    if (currentRoute.name === "workflows") {
      await refreshWorkflowsList();
      return;
    }
    if (currentRoute.name === "workflow-detail") {
      await refreshWorkflowDetail();
      return;
    }
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

  // ---- workflow list view ----------------------------------------------

  function fmtRelative(ts) {
    if (!ts) return "—";
    const d = new Date(ts);
    if (Number.isNaN(d.getTime())) return "—";
    const diffMs = Date.now() - d.getTime();
    if (diffMs < 0) return "in the future";
    const sec = Math.round(diffMs / 1000);
    if (sec < 60) return sec + " sec ago";
    const min = Math.round(sec / 60);
    if (min < 60) return min + " min ago";
    const hr = Math.round(min / 60);
    if (hr < 24) return hr + " hr ago";
    const day = Math.round(hr / 24);
    return day + " d ago";
  }

  function fmtDuration(startIso, endIso) {
    if (!startIso || !endIso) return null;
    const start = new Date(startIso).getTime();
    const end = new Date(endIso).getTime();
    if (Number.isNaN(start) || Number.isNaN(end) || end < start) return null;
    return fmtMs(end - start);
  }

  // Pre-baked sort comparators keyed by column. Falsy timestamps sink to
  // the bottom regardless of direction so empty cells don't ping-pong.
  const WF_STATUS_RANK = {
    running: 0,
    pending: 1,
    failed: 2,
    cancelled: 3,
    completed: 4,
  };

  function compareWorkflows(a, b, key, dir) {
    const sign = dir === "asc" ? 1 : -1;
    if (key === "status") {
      const ra = WF_STATUS_RANK[a.status] != null ? WF_STATUS_RANK[a.status] : 99;
      const rb = WF_STATUS_RANK[b.status] != null ? WF_STATUS_RANK[b.status] : 99;
      return (ra - rb) * sign;
    }
    if (key === "started") {
      const ta = a.started_at || a.created_at || "";
      const tb = b.started_at || b.created_at || "";
      if (!ta && !tb) return 0;
      if (!ta) return 1;
      if (!tb) return -1;
      return ta < tb ? -1 * sign : ta > tb ? 1 * sign : 0;
    }
    return 0;
  }

  function getFilteredSortedWorkflows() {
    let rows = wfListState.rows.slice();
    if (wfListState.filterStatus) {
      rows = rows.filter((r) => r.status === wfListState.filterStatus);
    }
    if (wfListState.filterWorkspace) {
      rows = rows.filter(
        (r) => r.workspace_id === wfListState.filterWorkspace,
      );
    }
    rows.sort((a, b) =>
      compareWorkflows(a, b, wfListState.sortKey, wfListState.sortDir),
    );
    return rows;
  }

  async function refreshWorkflowsList() {
    setSpinner(true);
    try {
      const data = await api("/api/workflows/overview");
      wfListState.rows = data.workflows || [];
      renderWorkflowsList(data);
      lastFetchAt = Date.now();
      tickRefreshLabel();
    } catch (err) {
      const body = $("#wf-list-body");
      if (body) {
        body.innerHTML = `<tr class="empty"><td colspan="6">failed: ${escapeHtml(
          err.message,
        )}</td></tr>`;
      }
      const meta = $("#last-refresh");
      if (meta) meta.textContent = "fetch failed: " + err.message;
    } finally {
      setSpinner(false);
    }
  }

  function renderWorkflowsList(data) {
    const body = $("#wf-list-body");
    const count = $("#wf-list-count");
    const summary = $("#wf-list-summary");
    if (!body) return;

    populateWorkspaceFilter(wfListState.rows);
    syncFilterSelections();
    syncSortHeaders();

    const rows = getFilteredSortedWorkflows();
    const total = (data && data.total) != null ? data.total : wfListState.rows.length;
    if (count) {
      const partial = data && data.partial ? " · partial" : "";
      count.textContent = `${intFmt.format(total)} total${partial}`;
    }
    if (summary) {
      const by = (data && data.by_status) || {};
      const parts = ["running", "completed", "failed", "cancelled", "pending"]
        .map((s) => `${s} ${intFmt.format(Number(by[s] || 0))}`)
        .join(" · ");
      summary.textContent = parts;
    }

    if (!rows.length) {
      body.innerHTML = `<tr class="empty"><td colspan="6">No workflows match the current filter.</td></tr>`;
      return;
    }

    body.innerHTML = rows
      .map((r) => {
        const status = r.status || "pending";
        const wsName = r.workspace_name || r.workspace_id || "—";
        const wfName = r.name || r.workflow_id || "—";
        const completed = Number(r.completed_count || 0);
        const total = Number(r.step_count || 0);
        const startedTs = r.started_at || r.created_at;
        const detailHref = `#/workflows/${encodeURIComponent(
          r.workflow_id || "",
        )}?ws=${encodeURIComponent(r.workspace_id || "")}`;
        return `
          <tr>
            <td>
              <span class="wf-list-status-icon wf-list-status-icon--${escapeHtml(
                status,
              )}" aria-hidden="true"></span>
              <span class="wf-list-status-label">${escapeHtml(status)}</span>
            </td>
            <td title="${escapeHtml(r.workspace_id || "")}">${escapeHtml(wsName)}</td>
            <td>
              <span class="wf-list-name">${escapeHtml(wfName)}</span>
              <span class="muted mono"> · ${escapeHtml(
                shortId(r.workflow_id, 10),
              )}</span>
            </td>
            <td class="num">${escapeHtml(
              intFmt.format(completed) + "/" + intFmt.format(total),
            )}</td>
            <td class="time" title="${escapeHtml(fmtDate(startedTs))}">${escapeHtml(
              fmtRelative(startedTs),
            )}</td>
            <td class="actions">
              <a class="btn small" href="${detailHref}">view</a>
            </td>
          </tr>
        `;
      })
      .join("");
  }

  function populateWorkspaceFilter(rows) {
    const select = $("#wf-filter-workspace");
    if (!select) return;
    const seen = new Map();
    for (const r of rows) {
      if (!r.workspace_id) continue;
      if (!seen.has(r.workspace_id)) {
        seen.set(r.workspace_id, r.workspace_name || r.workspace_id);
      }
    }
    const wanted = ["", ...seen.keys()].sort();
    const existing = $$("option", select).map((o) => o.value);
    if (
      wanted.length === existing.length &&
      wanted.every((v, i) => v === existing[i])
    ) {
      return;
    }
    select.innerHTML =
      `<option value="">all</option>` +
      Array.from(seen.entries())
        .sort((a, b) => String(a[1]).localeCompare(String(b[1])))
        .map(
          ([id, label]) =>
            `<option value="${escapeHtml(id)}">${escapeHtml(label)}</option>`,
        )
        .join("");
  }

  function syncFilterSelections() {
    const status = $("#wf-filter-status");
    if (status && status.value !== wfListState.filterStatus) {
      status.value = wfListState.filterStatus;
    }
    const workspace = $("#wf-filter-workspace");
    if (workspace && workspace.value !== wfListState.filterWorkspace) {
      workspace.value = wfListState.filterWorkspace;
    }
  }

  function syncSortHeaders() {
    $$(".wf-list-table th.sortable").forEach((th) => {
      const key = th.dataset.sort;
      if (key === wfListState.sortKey) {
        th.classList.add("sorted");
        th.setAttribute(
          "aria-sort",
          wfListState.sortDir === "asc" ? "ascending" : "descending",
        );
        th.dataset.dir = wfListState.sortDir;
      } else {
        th.classList.remove("sorted");
        th.removeAttribute("aria-sort");
        delete th.dataset.dir;
      }
    });
  }

  function wireWorkflowListControls() {
    const status = $("#wf-filter-status");
    if (status) {
      status.addEventListener("change", () => {
        wfListState.filterStatus = status.value || "";
        renderWorkflowsList({ workflows: wfListState.rows });
      });
    }
    const workspace = $("#wf-filter-workspace");
    if (workspace) {
      workspace.addEventListener("change", () => {
        wfListState.filterWorkspace = workspace.value || "";
        renderWorkflowsList({ workflows: wfListState.rows });
      });
    }
    $$(".wf-list-table th.sortable").forEach((th) => {
      th.addEventListener("click", () => {
        const key = th.dataset.sort;
        if (!key) return;
        if (wfListState.sortKey === key) {
          wfListState.sortDir = wfListState.sortDir === "asc" ? "desc" : "asc";
        } else {
          wfListState.sortKey = key;
          wfListState.sortDir = key === "started" ? "desc" : "asc";
        }
        renderWorkflowsList({ workflows: wfListState.rows });
      });
    });
  }

  // ---- workflow detail (graph) -----------------------------------------

  async function refreshWorkflowDetail() {
    const params = currentRoute.params || {};
    const wfId = params.wf_id;
    const wsId = params.ws_id;
    wfDetailState.wsId = wsId;
    wfDetailState.wfId = wfId;
    if (!wfId || !wsId) {
      const root = $("#wf-graph");
      if (root) {
        root.innerHTML = `<p class="empty muted">Missing ?ws=&lt;workspace_id&gt; in the URL.</p>`;
      }
      return;
    }
    if (wfDetailState.modalOpen) {
      // Pause auto-refresh while the user is reading a step modal.
      return;
    }
    setSpinner(true);
    try {
      const wf = await api(
        `/api/workspaces/${encodeURIComponent(wsId)}/workflows/${encodeURIComponent(
          wfId,
        )}`,
      );
      wfDetailState.workflow = wf;
      renderWorkflowDetailHead(wf);
      renderWorkflowGraph(wf);
      lastFetchAt = Date.now();
      tickRefreshLabel();
    } catch (err) {
      const root = $("#wf-graph");
      if (root) {
        root.innerHTML = `<p class="empty err">failed: ${escapeHtml(
          err.message,
        )}</p>`;
      }
    } finally {
      setSpinner(false);
    }
  }

  function renderWorkflowDetailHead(wf) {
    const nameEl = $("#wf-detail-name");
    const idEl = $("#wf-detail-id");
    const metaEl = $("#wf-detail-meta");
    const progressEl = $("#wf-detail-progress");
    if (!wf) return;
    if (nameEl) nameEl.textContent = wf.name || wf.id || "Workflow";
    if (idEl) idEl.textContent = wf.id || "";
    const status = wf.status || "pending";
    if (metaEl) {
      const created = wf.created_at ? fmtDate(wf.created_at) : "—";
      const started = wf.started_at ? fmtDate(wf.started_at) : "—";
      const finished = wf.finished_at ? fmtDate(wf.finished_at) : "—";
      metaEl.innerHTML = `
        <span class="workflow-status ${escapeHtml(status)}">${escapeHtml(
        status,
      )}</span>
        <span class="muted">workspace ${escapeHtml(wf.workspace_id || "")}</span>
        <span class="muted">created ${escapeHtml(created)}</span>
        <span class="muted">started ${escapeHtml(started)}</span>
        <span class="muted">finished ${escapeHtml(finished)}</span>
      `;
    }
    if (progressEl) {
      const manifest = wf.steps_manifest || [];
      const steps = wf.steps || [];
      const total = manifest.length || steps.length;
      const done = steps.filter((s) => s.status === "completed").length;
      progressEl.textContent = `${intFmt.format(done)}/${intFmt.format(total)} steps`;
    }
  }

  // Status → SVG-friendly icon + accessible label, kept distinct so colour
  // alone never conveys meaning (WCAG).
  const WF_STATUS_GLYPHS = {
    pending: { icon: "·", label: "pending" },
    running: { icon: "◐", label: "running" },
    completed: { icon: "✓", label: "completed" },
    failed: { icon: "✗", label: "failed" },
    cancelled: { icon: "—", label: "cancelled" },
  };

  function nodeKey(name) {
    return "wf-node-" + name.replace(/[^a-zA-Z0-9_-]/g, "_");
  }

  function buildNodeHtml(name, step) {
    const status = step ? step.status || "pending" : "pending";
    const glyph = WF_STATUS_GLYPHS[status] || WF_STATUS_GLYPHS.pending;
    const attempt = step && step.attempt ? Number(step.attempt) : 1;
    const duration = step
      ? fmtDuration(step.started_at, step.finished_at)
      : null;
    const labelParts = [];
    if (attempt > 1) labelParts.push(`attempt ${intFmt.format(attempt)}`);
    if (duration) labelParts.push(duration);
    const meta = labelParts.join(" · ");
    return `
      <div class="wf-node wf-node--${escapeHtml(status)}"
           role="listitem"
           tabindex="0"
           data-step="${escapeHtml(name)}"
           data-step-id="${escapeHtml(step ? step.id || "" : "")}"
           aria-label="${escapeHtml(name)} — ${escapeHtml(glyph.label)}">
        <span class="wf-node-name">${escapeHtml(name)}</span>
        <span class="wf-node-status">
          <span class="wf-node-icon" aria-hidden="true">${escapeHtml(glyph.icon)}</span>
          <span>${escapeHtml(glyph.label)}</span>
        </span>
        ${meta ? `<span class="wf-node-meta">${escapeHtml(meta)}</span>` : ""}
      </div>
    `;
  }

  function renderWorkflowGraph(wf) {
    const root = $("#wf-graph");
    if (!root) return;
    const manifest = (wf && wf.steps_manifest) || [];
    const steps = (wf && wf.steps) || [];

    // Build an ordered list of (name, latest-attempt) tuples. The manifest
    // is the source of truth for order; steps not listed in the manifest
    // (data drift) get appended at the end so we never lose information.
    const latestByName = new Map();
    for (const s of steps) {
      // Later entries supersede earlier ones (the workspace returns newest
      // attempt last).
      latestByName.set(s.name, s);
    }
    const ordered = [];
    for (const name of manifest) {
      ordered.push({ name, step: latestByName.get(name) || null });
    }
    for (const s of steps) {
      if (!manifest.includes(s.name)) {
        ordered.push({ name: s.name, step: s });
      }
    }

    if (!ordered.length) {
      root.innerHTML = `<p class="empty muted">No steps in this workflow yet.</p>`;
      return;
    }

    // Diff-based DOM update: re-use existing nodes when the data didn't
    // change (avoids the full innerHTML rewrite that causes flicker on
    // 5-second refresh).
    const html = ordered
      .map(({ name, step }, idx) => {
        const nodeHtml = buildNodeHtml(name, step);
        const edge =
          idx < ordered.length - 1
            ? `<div class="wf-edge" aria-hidden="true"></div>`
            : "";
        return `<div class="wf-graph-cell" data-key="${escapeHtml(
          nodeKey(name),
        )}">${nodeHtml}${edge}</div>`;
      })
      .join("");

    // Compare new vs current children by data-key + status to skip
    // unnecessary work when the auto-refresh tick brings no changes.
    const current = root.firstElementChild;
    const looksUnchanged = current && root.dataset.signature === graphSignature(ordered);
    if (looksUnchanged) return;
    root.dataset.signature = graphSignature(ordered);
    root.innerHTML = html;
  }

  function graphSignature(ordered) {
    return ordered
      .map(({ name, step }) =>
        [
          name,
          step ? step.status || "pending" : "pending",
          step ? step.attempt || 1 : 1,
          step ? step.finished_at || "" : "",
          step ? step.started_at || "" : "",
        ].join("|"),
      )
      .join("⇒");
  }

  async function openWorkflowStepModal(stepName) {
    const wf = wfDetailState.workflow;
    if (!wf || !stepName) return;
    const steps = wf.steps || [];
    const matches = steps.filter((s) => s.name === stepName);
    const step = matches.length ? matches[matches.length - 1] : null;

    const modal = $("#wf-step-modal");
    const titleEl = $("#wf-step-modal-title");
    const bodyEl = $("#wf-step-modal-body");
    if (!modal || !bodyEl) return;
    wfDetailState.modalOpen = true;
    if (titleEl) {
      titleEl.textContent = step
        ? `${stepName} — ${step.status || "pending"}`
        : `${stepName} — pending`;
    }
    modal.hidden = false;

    if (!step) {
      bodyEl.innerHTML = `<p class="empty muted">This step has not started yet.</p>`;
      return;
    }

    const duration = fmtDuration(step.started_at, step.finished_at);
    const lines = [
      ["Step ID", step.id || "—"],
      ["Status", step.status || "pending"],
      ["Attempt", String(step.attempt != null ? step.attempt : 1)],
      ["Started", step.started_at ? fmtDate(step.started_at) : "—"],
      ["Finished", step.finished_at ? fmtDate(step.finished_at) : "—"],
      ["Duration", duration || "—"],
      ["Snapshot ID", step.snapshot_id || "—"],
    ];
    const meta = lines
      .map(
        ([k, v]) =>
          `<div class="wf-step-row"><span class="wf-step-key">${escapeHtml(
            k,
          )}</span><span class="wf-step-val">${escapeHtml(String(v))}</span></div>`,
      )
      .join("");

    const inputJson = jsonBlock("Input", step.input);
    const outputJson = jsonBlock("Output", step.output);
    const errorBlock = step.error
      ? `<div class="wf-step-section">
           <h3>Error</h3>
           <pre class="wf-step-error">${escapeHtml(String(step.error))}</pre>
         </div>`
      : "";

    bodyEl.innerHTML = `
      <div class="wf-step-meta">${meta}</div>
      ${inputJson}
      ${outputJson}
      ${errorBlock}
      <div class="wf-step-section" id="wf-step-lease-section" hidden></div>
    `;

    // Best-effort lease lookup: only meaningful for running steps. We
    // don't fail the whole modal if this errors.
    if (step.status === "running") {
      void renderLeaseInfo(step.id);
    }
  }

  async function renderLeaseInfo(stepId) {
    const root = $("#wf-step-lease-section");
    if (!root || !stepId) return;
    const wsId = wfDetailState.wsId;
    const wfId = wfDetailState.wfId;
    if (!wsId || !wfId) return;
    try {
      const data = await api(
        `/api/workspaces/${encodeURIComponent(wsId)}/workflows/${encodeURIComponent(
          wfId,
        )}`,
      );
      const steps = (data && data.steps) || [];
      const step = steps.find((s) => s.id === stepId) || null;
      const lease = step && step.lease ? step.lease : null;
      if (!lease) return;
      root.hidden = false;
      const rows = [
        ["Worker", lease.worker_id || "—"],
        ["Acquired", lease.acquired_at ? fmtDate(lease.acquired_at) : "—"],
        ["Heartbeat", lease.heartbeat_at ? fmtDate(lease.heartbeat_at) : "—"],
        ["Expires", lease.expires_at ? fmtDate(lease.expires_at) : "—"],
      ];
      root.innerHTML =
        `<h3>Lease</h3>` +
        rows
          .map(
            ([k, v]) =>
              `<div class="wf-step-row"><span class="wf-step-key">${escapeHtml(
                k,
              )}</span><span class="wf-step-val">${escapeHtml(
                String(v),
              )}</span></div>`,
          )
          .join("");
    } catch (_) {
      // Silent: a missing lease isn't an error worth surfacing.
    }
  }

  function jsonBlock(label, value) {
    if (value == null) return "";
    let pretty;
    try {
      pretty = JSON.stringify(value, null, 2);
    } catch (_) {
      pretty = String(value);
    }
    return `
      <details class="wf-step-section" open>
        <summary><h3>${escapeHtml(label)}</h3></summary>
        <pre class="wf-step-json">${escapeHtml(pretty)}</pre>
      </details>
    `;
  }

  function closeWorkflowStepModal({ silent } = {}) {
    const modal = $("#wf-step-modal");
    if (modal) modal.hidden = true;
    if (wfDetailState.modalOpen && !silent) {
      // Resume polling immediately when the user dismisses the modal so
      // the freshest data shows up without waiting another 5 seconds.
      wfDetailState.modalOpen = false;
      if (currentRoute.name === "workflow-detail") {
        void refreshWorkflowDetail();
      }
    } else {
      wfDetailState.modalOpen = false;
    }
  }

  // ---- bootstrap --------------------------------------------------------

  document.addEventListener("DOMContentLoaded", () => {
    const versionEl = $("#dashboard-version");
    if (versionEl) versionEl.textContent = "0.1.0";

    const refreshBtn = $("#refresh-btn");
    if (refreshBtn) refreshBtn.addEventListener("click", () => {
      if (currentRoute.name === "workspace") {
        void loadWorkspace(currentRoute.params.ws_id);
      } else if (currentRoute.name === "workflows") {
        void refreshWorkflowsList();
      } else if (currentRoute.name === "workflow-detail") {
        void refreshWorkflowDetail();
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

    // Dead-letter + workflow-step modals: delegate clicks across both.
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
      // Workflow graph node click → step modal.
      const node = target.closest(".wf-node");
      if (node instanceof HTMLElement) {
        event.preventDefault();
        const stepName = node.dataset.step || "";
        if (stepName) void openWorkflowStepModal(stepName);
        return;
      }
      // Step modal: close button or backdrop.
      if (
        target.id === "wf-step-modal-close" ||
        target.id === "wf-step-modal" ||
        target.classList.contains("wf-step-modal")
      ) {
        closeWorkflowStepModal();
        return;
      }
      // DLQ modal: bulk-action buttons take precedence over the
      // close-on-backdrop check below — otherwise a stray click anywhere
      // inside ``.dlq-modal`` would dismiss the modal mid-action.
      if (target.id === "dlq-replay-all" || target.closest("#dlq-replay-all")) {
        event.preventDefault();
        void replayAllDeadletters();
        return;
      }
      if (target.id === "dlq-purge-old" || target.closest("#dlq-purge-old")) {
        event.preventDefault();
        void purgeOldDeadletters();
        return;
      }
      // DLQ modal: close button or backdrop.
      if (
        target.id === "dlq-modal-close" ||
        target.id === "dlq-modal" ||
        target.classList.contains("dlq-modal")
      ) {
        closeDlqModal();
      }
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        closeDlqModal();
        closeWorkflowStepModal();
      }
      // Activate a focused graph node with Enter / Space.
      if ((event.key === "Enter" || event.key === " ") && document.activeElement) {
        const node = document.activeElement.closest
          ? document.activeElement.closest(".wf-node")
          : null;
        if (node instanceof HTMLElement) {
          event.preventDefault();
          const stepName = node.dataset.step || "";
          if (stepName) void openWorkflowStepModal(stepName);
        }
      }
    });

    window.addEventListener("hashchange", navigate);
    navigate();
  });
})();
