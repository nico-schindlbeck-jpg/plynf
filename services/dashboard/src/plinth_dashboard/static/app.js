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
        // v1.5 — /workflows/<id>/replay route. Re-uses the same `?ws=`
        // query convention as the detail page so deep-links work.
        if (parts[2] === "replay") {
          return {
            name: "workflow-replay",
            params: {
              wf_id: parts[1],
              ws_id: qs.get("ws") || "",
            },
          };
        }
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
    if (parts[0] === "studio") {
      // v1.5 — Plinth Studio (visual workflow builder).
      return { name: "studio", params: {} };
    }
    if (parts[0] === "tenants") {
      if (parts[1]) {
        return { name: "tenant-detail", params: { tenant_id: parts[1] } };
      }
      return { name: "tenants", params: {} };
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
    } else if (route.name === "workflow-replay") {
      crumb.textContent =
        "/ workflows / " + (route.params.wf_id || "") + " / replay";
    } else if (route.name === "studio") {
      crumb.textContent = "/ studio";
    } else if (route.name === "tenants") {
      crumb.textContent = "/ tenants";
    } else if (route.name === "tenant-detail") {
      crumb.textContent = "/ tenants / " + (route.params.tenant_id || "");
    } else {
      crumb.textContent = "";
    }
  }

  function renderTopnav(route) {
    // Highlight whichever top-level area the current route belongs to.
    const links = $$(".topnav-link");
    let active = "overview";
    if (
      route.name === "workflows" ||
      route.name === "workflow-detail" ||
      route.name === "workflow-replay"
    ) {
      active = "workflows";
    } else if (route.name === "studio") {
      active = "studio";
    } else if (route.name === "tenants" || route.name === "tenant-detail") {
      active = "tenants";
    }
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
    // v1.0 — stop tile polling whenever we leave overview; restart in the
    // overview branch below.
    stopTimeseriesTiles();
    // v1.4 — same for cost-by-agent + anomalies.
    stopCostAnomalyPolling();
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
    } else if (route.name === "workflow-replay") {
      mountTemplate("tpl-workflow-replay");
      stopPolling();
      void loadWorkflowReplay();
    } else if (route.name === "studio") {
      mountTemplate("tpl-studio-v2");
      stopPolling();
      void loadStudio();
    } else if (route.name === "tenants") {
      mountTemplate("tpl-tenants-list");
      stopPolling();
      void loadTenantsList();
    } else if (route.name === "tenant-detail") {
      mountTemplate("tpl-tenant-detail");
      stopPolling();
      void loadTenantDetail(route.params.tenant_id);
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

  // ---- v1.0 time-series tiles -----------------------------------------
  //
  // Each tile fetches /api/timeseries?metric=...&window=24h independently so
  // a slow/failed metric doesn't block the others. We refresh on the same
  // 30s tempo regardless of the main overview poll cadence.

  const TS_TILE_METRICS = ["cost", "latency_p99", "error_rate", "cache_hit_ratio"];
  const TS_TILE_REFRESH_MS = 30000;
  let tsTilesTimer = null;

  function fmtTsValue(metric, v) {
    const n = Number(v || 0);
    if (metric === "cost") return "$" + n.toFixed(4);
    if (metric === "latency_p99") {
      if (n >= 1000) return (n / 1000).toFixed(2) + "s";
      return Math.round(n) + "ms";
    }
    if (metric === "error_rate" || metric === "cache_hit_ratio") {
      return n.toFixed(1) + "%";
    }
    return String(Math.round(n));
  }

  function renderTimeseriesTile(tile, payload) {
    const svgRoot = tile.querySelector("[data-svg]");
    const summary = tile.querySelector("[data-summary]");
    if (!svgRoot) return;
    const points = (payload && payload.points) || [];
    const sum = (payload && payload.summary) || { min: 0, max: 0, avg: 0 };
    const metric = tile.dataset.metric;
    if (summary) {
      summary.textContent = points.length
        ? "min " + fmtTsValue(metric, sum.min)
            + " · avg " + fmtTsValue(metric, sum.avg)
            + " · max " + fmtTsValue(metric, sum.max)
        : "no data";
    }

    if (!points.length) {
      svgRoot.innerHTML = '<svg viewBox="0 0 300 100" preserveAspectRatio="none"></svg>';
      return;
    }

    const width = 300;
    const height = 100;
    const padX = 4;
    const padY = 6;
    const innerW = width - padX * 2;
    const innerH = height - padY * 2;

    const values = points.map((p) => Number(p.value || 0));
    const maxV = Math.max.apply(null, values);
    const minV = Math.min.apply(null, values);
    const span = Math.max(maxV - minV, 0.000001);

    function xFor(i) {
      return padX + (i / Math.max(1, values.length - 1)) * innerW;
    }
    function yFor(v) {
      const norm = (v - minV) / span;
      return padY + (1 - norm) * innerH;
    }

    let pathD = "";
    values.forEach((v, i) => {
      const x = xFor(i);
      const y = yFor(v);
      pathD += (i === 0 ? "M" : " L") + x.toFixed(2) + "," + y.toFixed(2);
    });

    let areaD = pathD
      + " L" + xFor(values.length - 1).toFixed(2) + "," + (padY + innerH).toFixed(2)
      + " L" + xFor(0).toFixed(2) + "," + (padY + innerH).toFixed(2)
      + " Z";

    const svg =
      '<svg viewBox="0 0 ' + width + ' ' + height + '" preserveAspectRatio="none">'
        + '<line class="ts-axis" x1="' + padX + '" y1="' + (height - padY) + '" '
              + 'x2="' + (width - padX) + '" y2="' + (height - padY) + '" />'
        + '<path class="ts-area" d="' + areaD + '" />'
        + '<path class="ts-line" d="' + pathD + '" />'
      + '</svg>';
    svgRoot.innerHTML = svg;
  }

  async function refreshTimeseriesTiles() {
    if (currentRoute.name !== "overview") return;
    const tiles = document.querySelectorAll(".ts-tile[data-metric]");
    if (!tiles.length) return;
    await Promise.all(Array.from(tiles).map(async (tile) => {
      const metric = tile.dataset.metric;
      try {
        const payload = await api(
          "/api/timeseries?metric=" + encodeURIComponent(metric) + "&window=24h"
        );
        renderTimeseriesTile(tile, payload);
      } catch (e) {
        renderTimeseriesTile(tile, { points: [], summary: { min: 0, max: 0, avg: 0 } });
      }
    }));
  }

  function startTimeseriesTiles() {
    if (tsTilesTimer) clearInterval(tsTilesTimer);
    refreshTimeseriesTiles();
    tsTilesTimer = setInterval(refreshTimeseriesTiles, TS_TILE_REFRESH_MS);
  }

  function stopTimeseriesTiles() {
    if (tsTilesTimer) clearInterval(tsTilesTimer);
    tsTilesTimer = null;
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

  // ---- v1.4 cost-by-agent + anomalies ---------------------------------
  //
  // These two panels each have their own 30-second polling cadence — both
  // queries are heavier than the overview, so they ride on a separate
  // interval. The state below carries the last-seen sort + the timer
  // handle so navigation in/out of overview cleans up properly.

  const COST_ANOMALY_REFRESH_MS = 30000;
  let costByAgentTimer = null;
  let anomaliesTimer = null;
  const costByAgentState = {
    sortKey: "total_cost_usd",
    sortDir: "desc", // "asc" | "desc"
  };

  function fmtZ(z) {
    const n = Number(z || 0);
    const sign = n >= 0 ? "+" : "";
    return sign + n.toFixed(2);
  }

  function severityClass(sev) {
    if (sev === "critical") return "sev-critical";
    if (sev === "warning") return "sev-warning";
    return "sev-info";
  }

  function severityGlyph(sev) {
    if (sev === "critical") return "✗";
    if (sev === "warning") return "!";
    return "i";
  }

  function renderTopToolStack(tools) {
    const arr = (tools || []).slice(0, 5);
    if (!arr.length) return '<span class="muted">—</span>';
    const total = arr.reduce((acc, t) => acc + Number(t.cost_usd || 0), 0);
    if (total <= 0) {
      // No cost — render as flat tags so the row still shows the tools.
      return arr
        .map(
          (t) =>
            `<span class="cba-stack-flat" title="${escapeHtml(t.tool_id)}">${escapeHtml(
              t.tool_id,
            )}</span>`,
        )
        .join("");
    }
    const segs = arr
      .map((t) => {
        const pct = Math.max(2, Math.round((Number(t.cost_usd || 0) / total) * 100));
        const tip =
          `${t.tool_id}: $${Number(t.cost_usd || 0).toFixed(4)} · ` +
          `${intFmt.format(t.invocations || 0)} calls`;
        return `<span class="cba-seg" style="width:${pct}%" title="${escapeHtml(tip)}"
          data-tool="${escapeHtml(t.tool_id)}"></span>`;
      })
      .join("");
    return `<div class="cba-stack">${segs}</div>`;
  }

  function renderCostByAgent(report) {
    const body = $("#cost-by-agent-body");
    const summary = $("#cost-by-agent-summary");
    if (!body) return;
    const agents = (report && report.agents) || [];
    if (summary) {
      const tot = report ? report.total_agents || 0 : 0;
      const totCost = report ? report.total_cost_usd || 0 : 0;
      summary.textContent =
        `${intFmt.format(tot)} agent${tot === 1 ? "" : "s"} · `
        + `${fmtUSD(totCost)} total · refresh every 30s`;
    }
    if (!agents.length) {
      body.innerHTML = `<tr class="empty"><td colspan="8">No invocations yet.</td></tr>`;
      return;
    }
    const sorted = agents.slice().sort((a, b) => {
      const k = costByAgentState.sortKey;
      const av = Number(a[k] || 0);
      const bv = Number(b[k] || 0);
      const cmp = av - bv;
      return costByAgentState.sortDir === "asc" ? cmp : -cmp;
    });
    body.innerHTML = sorted
      .map((a) => {
        const tools = renderTopToolStack(a.top_tools);
        const cached = `${intFmt.format(a.cached_invocations || 0)}/${intFmt.format(a.invocations || 0)}`;
        return `
        <tr data-agent-id="${escapeHtml(a.agent_id)}">
          <td class="id mono" title="${escapeHtml(a.agent_id)}">${escapeHtml(shortId(a.agent_id, 14))}</td>
          <td class="muted">${escapeHtml(a.tenant_id || "default")}</td>
          <td class="num">${escapeHtml(intFmt.format(a.invocations || 0))}</td>
          <td class="num muted">${escapeHtml(cached)}</td>
          <td class="num">${escapeHtml(fmtMs(Math.round(a.avg_duration_ms || 0)))}</td>
          <td class="num">${escapeHtml(fmtUSD(a.total_cost_usd || 0))}</td>
          <td class="cba-tools">${tools}</td>
          <td class="actions">
            <a class="btn small cba-drilldown" href="#/" data-agent-id="${escapeHtml(a.agent_id)}">audit</a>
          </td>
        </tr>`;
      })
      .join("");

    // Wire sort handles + drill-down (dynamic rebind on every render is
    // fine — the table is small and the listeners are trivial).
    $$('th.sortable[data-sort]', $('.cost-by-agent-table'))
      .forEach((th) => {
        th.onclick = () => {
          const next = th.dataset.sort;
          if (costByAgentState.sortKey === next) {
            costByAgentState.sortDir =
              costByAgentState.sortDir === "asc" ? "desc" : "asc";
          } else {
            costByAgentState.sortKey = next;
            costByAgentState.sortDir = "desc";
          }
          // Trigger a re-render with the cached payload (no fetch).
          renderCostByAgent(report);
        };
      });

    $$('a.cba-drilldown', body).forEach((a) => {
      a.onclick = (ev) => {
        ev.preventDefault();
        const aid = a.dataset.agentId;
        if (!aid) return;
        // Drill-down: fetch + dump the per-agent audit into a modal.
        openCostByAgentDrilldown(aid);
      };
    });
  }

  async function openCostByAgentDrilldown(agentId) {
    // The drill-down reuses the existing /api/audit proxy which already
    // accepts agent_id. We open a tiny dialog (vanilla; no template) so
    // the user can scan the rows without leaving the overview.
    const existing = document.getElementById("cba-drilldown-modal");
    if (existing) existing.remove();
    const modal = document.createElement("div");
    modal.id = "cba-drilldown-modal";
    modal.className = "dlq-modal";
    modal.hidden = false;
    modal.innerHTML = `
      <div class="dlq-modal-card">
        <div class="dlq-modal-head">
          <h2>Audit · agent ${escapeHtml(agentId)}</h2>
          <button class="btn small" type="button" data-close>close</button>
        </div>
        <div class="dlq-modal-body">
          <p class="empty muted">loading…</p>
        </div>
      </div>`;
    document.body.appendChild(modal);
    modal.querySelector("[data-close]").onclick = () => modal.remove();

    try {
      const data = await api(
        "/api/audit?limit=100&agent_id=" + encodeURIComponent(agentId),
      );
      const events = data.events || [];
      const body = modal.querySelector(".dlq-modal-body");
      if (!events.length) {
        body.innerHTML = `<p class="empty muted">No events for this agent.</p>`;
        return;
      }
      body.innerHTML =
        '<table class="data-table"><thead><tr>' +
        '<th>Time</th><th>Tool</th><th>Cached</th>' +
        '<th class="num">Duration</th><th class="num">Cost</th>' +
        '</tr></thead><tbody>' +
        events
          .map(
            (e) => `
            <tr>
              <td class="time">${escapeHtml(fmtTime(e.timestamp))}</td>
              <td class="tool">${escapeHtml(e.tool_id || "?")}</td>
              <td>${e.cached ? '<span class="tag cached">yes</span>' : '<span class="tag fresh">no</span>'}</td>
              <td class="num">${escapeHtml(fmtMs(e.duration_ms))}</td>
              <td class="num">${escapeHtml(fmtUSD(e.cost_estimate_usd))}</td>
            </tr>`,
          )
          .join("") +
        "</tbody></table>";
    } catch (err) {
      const body = modal.querySelector(".dlq-modal-body");
      if (body) body.innerHTML = `<p class="empty err">failed: ${escapeHtml(err.message)}</p>`;
    }
  }

  function buildAnomalySparkline(samples) {
    const arr = (samples || []).map((v) => Number(v || 0));
    if (!arr.length) return "";
    const width = 60;
    const height = 18;
    const padX = 1;
    const padY = 1;
    const innerW = width - padX * 2;
    const innerH = height - padY * 2;
    const maxV = Math.max.apply(null, arr);
    const minV = Math.min.apply(null, arr);
    const span = Math.max(maxV - minV, 0.000001);
    function xFor(i) {
      return padX + (i / Math.max(1, arr.length - 1)) * innerW;
    }
    function yFor(v) {
      const norm = (v - minV) / span;
      return padY + (1 - norm) * innerH;
    }
    let pathD = "";
    arr.forEach((v, i) => {
      const x = xFor(i).toFixed(2);
      const y = yFor(v).toFixed(2);
      pathD += (i === 0 ? "M" : " L") + x + "," + y;
    });
    return (
      '<svg class="anomaly-spark" viewBox="0 0 ' + width + " " + height + '"' +
      ' preserveAspectRatio="none">' +
      '<path class="anomaly-spark-line" d="' + pathD + '" />' +
      "</svg>"
    );
  }

  function renderAnomalies(report) {
    const root = $("#anomalies-list");
    const summary = $("#anomalies-summary");
    if (!root) return;
    const anomalies = (report && report.anomalies) || [];
    if (summary) {
      const total = report ? report.total_anomalies || 0 : 0;
      const by = (report && report.by_severity) || {};
      const parts = [];
      if (by.critical) parts.push(`${by.critical} critical`);
      if (by.warning) parts.push(`${by.warning} warning`);
      if (by.info) parts.push(`${by.info} info`);
      summary.textContent =
        total === 0
          ? "no anomalies · refresh every 30s"
          : `${intFmt.format(total)} (${parts.join(" · ")}) · refresh every 30s`;
    }
    if (!anomalies.length) {
      root.innerHTML = `<p class="empty muted">No anomalies detected.</p>`;
      return;
    }
    root.innerHTML = anomalies
      .map((a, idx) => {
        const sevCls = severityClass(a.severity);
        const sevGlyph = severityGlyph(a.severity);
        const dims = [];
        if (a.agent_id) dims.push(`agent ${a.agent_id}`);
        if (a.tool_id) dims.push(`tool ${a.tool_id}`);
        if (a.tenant_id) dims.push(`tenant ${a.tenant_id}`);
        const dimsHtml = dims.length
          ? `<span class="anomaly-dims muted">${escapeHtml(dims.join(" · "))}</span>`
          : "";
        const baselineSamples =
          (a.raw_data && a.raw_data.baseline_samples) || [];
        const spark = buildAnomalySparkline(baselineSamples);
        const valueHtml =
          `<span class="anomaly-metric">${escapeHtml(a.metric_name || "")}</span> ` +
          `<strong>${escapeHtml(Number(a.metric_value || 0).toFixed(4))}</strong> ` +
          `<span class="muted">vs μ=${escapeHtml(Number(a.baseline_mean || 0).toFixed(4))} · z=${escapeHtml(fmtZ(a.z_score))}</span>`;
        return `
          <details class="anomaly-row ${sevCls}" data-id="${escapeHtml(a.id)}" ${idx < 3 ? "open" : ""}>
            <summary>
              <span class="anomaly-glyph ${sevCls}">${sevGlyph}</span>
              <span class="anomaly-type">${escapeHtml(a.type)}</span>
              <span class="anomaly-desc">${escapeHtml(a.description)}</span>
              <span class="anomaly-time muted">${escapeHtml(fmtTime(a.detected_at))}</span>
            </summary>
            <div class="anomaly-detail">
              <div class="anomaly-line">${valueHtml}</div>
              ${dimsHtml}
              <div class="anomaly-spark-wrap">${spark}</div>
            </div>
          </details>
        `;
      })
      .join("");
  }

  async function refreshCostByAgent() {
    if (currentRoute.name !== "overview") return;
    try {
      const data = await api("/api/cost-by-agent?window=24h&top=10");
      renderCostByAgent(data);
    } catch (err) {
      const body = $("#cost-by-agent-body");
      if (body) {
        body.innerHTML = `<tr class="empty err"><td colspan="8">fetch failed: ${escapeHtml(
          err.message,
        )}</td></tr>`;
      }
    }
  }

  async function refreshAnomalies() {
    if (currentRoute.name !== "overview") return;
    try {
      const data = await api("/api/anomalies?window=1h&min_severity=info");
      renderAnomalies(data);
    } catch (err) {
      const root = $("#anomalies-list");
      if (root) {
        root.innerHTML = `<p class="empty err">fetch failed: ${escapeHtml(
          err.message,
        )}</p>`;
      }
    }
  }

  function startCostAnomalyPolling() {
    if (costByAgentTimer) clearInterval(costByAgentTimer);
    if (anomaliesTimer) clearInterval(anomaliesTimer);
    void refreshCostByAgent();
    void refreshAnomalies();
    costByAgentTimer = setInterval(
      () => void refreshCostByAgent(),
      COST_ANOMALY_REFRESH_MS,
    );
    anomaliesTimer = setInterval(
      () => void refreshAnomalies(),
      COST_ANOMALY_REFRESH_MS,
    );
  }

  function stopCostAnomalyPolling() {
    if (costByAgentTimer) clearInterval(costByAgentTimer);
    if (anomaliesTimer) clearInterval(anomaliesTimer);
    costByAgentTimer = null;
    anomaliesTimer = null;
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
      // v1.0 — kick off tile refresh on first overview render. The tiles
      // own their own 30s polling cadence so they don't compete with the
      // main 5s overview poll.
      if (!tsTilesTimer) startTimeseriesTiles();
      // v1.4 — same pattern for cost-by-agent + anomalies.
      if (!costByAgentTimer) startCostAnomalyPolling();

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
        <td class="actions">
          <button class="btn small" type="button"
                  data-channel-edit-schema="${escapeHtml(c.name || "")}">
            Edit schema
          </button>
        </td>
      </tr>`,
    });
    // Wire the per-row "Edit schema" buttons after rendering.
    const body = $("#channel-body");
    if (body) {
      body.querySelectorAll("[data-channel-edit-schema]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const name = btn.getAttribute("data-channel-edit-schema") || "";
          const wsId = currentRoute.params.ws_id;
          if (!wsId || !name) return;
          // Fetch the existing schema (may 404 → null) so the editor pre-fills.
          let existing = null;
          try {
            const r = await fetch(
              `/api/workspaces/${encodeURIComponent(wsId)}/channels/${encodeURIComponent(name)}/schema`,
            );
            if (r.ok) {
              const body = await r.json();
              existing = body.schema_json || body.schema || null;
            }
          } catch (_) {
            /* no-op */
          }
          if (window.PlinthSchemaWizard) {
            window.PlinthSchemaWizard.open(wsId, name, existing);
          }
        });
      });
    }
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
      } else if (currentRoute.name === "workflow-replay") {
        void loadWorkflowReplay();
      } else if (currentRoute.name === "studio") {
        void loadStudio();
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

  // ---- v1.0: Tenants admin UI -----------------------------------------

  // Quota presets used by the create-tenant modal. ``default`` matches
  // the contract; trial / enterprise are 10% and 10x of every numeric
  // limit (rounded sensibly).
  const QUOTA_PRESETS = {
    default: null,  // server-side defaults take over.
    trial: {
      max_workspaces: 10,
      max_storage_gb: 1.0,
      max_channels_per_workspace: 5,
      max_workflows_per_workspace: 10,
      max_active_tokens: 100,
      max_oauth_connections: 5,
      max_cost_usd_day: 10.0,
      max_cost_usd_month: 200.0,
      max_invocations_per_minute: 60,
    },
    enterprise: {
      max_workspaces: 1000,
      max_storage_gb: 100.0,
      max_channels_per_workspace: 500,
      max_workflows_per_workspace: 1000,
      max_active_tokens: 10000,
      max_oauth_connections: 500,
      max_cost_usd_day: 1000.0,
      max_cost_usd_month: 20000.0,
      max_invocations_per_minute: 6000,
    },
  };

  async function loadTenantsList() {
    const body = $("#tenants-list-body");
    const countEl = $("#tenants-list-count");
    if (!body) return;
    body.innerHTML = `<tr class="empty"><td colspan="6">loading&hellip;</td></tr>`;

    let tenants = [];
    let costByTenant = {};
    try {
      const list = await api("/api/tenants");
      tenants = list.tenants || [];
    } catch (err) {
      body.innerHTML = `<tr class="empty"><td colspan="6">Failed to load: ${escapeHtml(
        err.message,
      )}</td></tr>`;
      return;
    }
    try {
      // Best-effort: use the overview's audit rollup for cost-by-tenant.
      const ov = await api("/api/overview");
      const list = (ov.tenants || {}).list || [];
      costByTenant = list.reduce((acc, t) => {
        acc[t.id] = Number(t.cost_24h || 0);
        return acc;
      }, {});
    } catch (_) {
      // Silent — costs simply show as $0 if the overview is unreachable.
    }

    if (countEl) countEl.textContent = `${tenants.length} total`;
    if (!tenants.length) {
      body.innerHTML = `<tr class="empty"><td colspan="6">No tenants yet.</td></tr>`;
      return;
    }
    body.innerHTML = tenants
      .map((t) => {
        const cost = costByTenant[t.id] || 0;
        return `
          <tr>
            <td class="id mono">${escapeHtml(t.id)}</td>
            <td>${escapeHtml(safe(t.name, t.id))}</td>
            <td class="num">${intFmt.format(t.member_count || 0)}</td>
            <td class="num">${intFmt.format(t.workspace_count || 0)}</td>
            <td class="num">${escapeHtml(fmtUSD(cost))}</td>
            <td class="actions">
              <a class="btn small" href="#/tenants/${encodeURIComponent(
                t.id,
              )}">view</a>
            </td>
          </tr>
        `;
      })
      .join("");

    // Wire create-tenant button.
    const createBtn = $("#tenant-create-btn");
    if (createBtn) {
      createBtn.onclick = openTenantCreateModal;
    }
  }

  function openTenantCreateModal() {
    const m = $("#tenant-create-modal");
    if (!m) return;
    m.hidden = false;
    const form = $("#tenant-create-form");
    const status = $("#tenant-create-status");
    if (status) status.textContent = "";
    if (!form) return;
    form.onsubmit = async (e) => {
      e.preventDefault();
      const data = new FormData(form);
      const id = String(data.get("id") || "").trim();
      const name = String(data.get("name") || "").trim();
      const preset = String(data.get("preset") || "default");
      if (!id || !name) return;
      try {
        const r = await fetch("/api/tenants", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id, name }),
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        // Optionally apply a preset.
        const presetBody = QUOTA_PRESETS[preset];
        if (presetBody) {
          await fetch(`/api/tenants/${encodeURIComponent(id)}/quotas`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(presetBody),
          });
        }
        if (status) status.textContent = "Created.";
        location.hash = `#/tenants/${encodeURIComponent(id)}`;
      } catch (err) {
        if (status) status.textContent = `Failed: ${err.message}`;
      }
    };
    const closeBtn = $("#tenant-create-close");
    if (closeBtn) {
      closeBtn.onclick = () => {
        m.hidden = true;
      };
    }
  }

  async function loadTenantDetail(tenantId) {
    if (!tenantId) return;
    const idEl = $("#tenant-id");
    if (idEl) idEl.textContent = tenantId;
    const nameEl = $("#tenant-name");

    let tenant = null;
    try {
      tenant = await api(`/api/tenants/${encodeURIComponent(tenantId)}`);
    } catch (err) {
      if (nameEl) nameEl.textContent = `Tenant (${err.message})`;
    }
    if (nameEl) nameEl.textContent = (tenant && tenant.name) || tenantId;

    // Quotas
    let quotas = null;
    try {
      quotas = await api(`/api/tenants/${encodeURIComponent(tenantId)}/quotas`);
    } catch (_) {
      /* identity may be unreachable; form stays blank */
    }
    populateQuotasForm(quotas);
    wireQuotasForm(tenantId);

    // Usage
    void renderTenantUsage(tenantId, quotas);

    // Audit (filter by tenant_id)
    void renderTenantAudit(tenantId);

    // Delete
    const delBtn = $("#tenant-delete-btn");
    if (delBtn) {
      delBtn.onclick = () => {
        if (
          confirm(
            "Delete tenant " +
              tenantId +
              "?\n\nThis is hard. Run a GDPR export first if you need the data.",
          )
        ) {
          // For now we surface a CONTRACTS-aligned "use the API" message;
          // full hard-delete cascade lives behind /v1/tenants/{id}/data
          // which is parallel-agent territory.
          alert("Hard-delete must be done via the GDPR endpoint at this time.");
        }
      };
    }
  }

  function populateQuotasForm(quotas) {
    const form = $("#tenant-quotas-form");
    if (!form) return;
    if (!quotas) return;
    Array.from(form.elements).forEach((el) => {
      if (el.name && Object.prototype.hasOwnProperty.call(quotas, el.name)) {
        el.value = String(quotas[el.name]);
      }
    });
  }

  function wireQuotasForm(tenantId) {
    const form = $("#tenant-quotas-form");
    if (!form) return;
    const status = $("#quotas-status");
    form.onsubmit = async (e) => {
      e.preventDefault();
      const data = new FormData(form);
      const body = {};
      for (const [k, v] of data.entries()) {
        if (v === "" || v == null) continue;
        body[k] = Number(v);
      }
      try {
        const r = await fetch(
          `/api/tenants/${encodeURIComponent(tenantId)}/quotas`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          },
        );
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        if (status) status.textContent = "Saved.";
        // Refresh usage display with the latest quota values.
        const quotas = await r.json();
        populateQuotasForm(quotas);
        await renderTenantUsage(tenantId, quotas);
      } catch (err) {
        if (status) status.textContent = `Failed: ${err.message}`;
      }
    };
    const reset = $("#quotas-reset");
    if (reset) {
      reset.onclick = async () => {
        if (!confirm("Reset all quotas for " + tenantId + "?")) return;
        try {
          await fetch(
            `/api/tenants/${encodeURIComponent(tenantId)}/quotas`,
            { method: "DELETE" },
          );
          // Reload defaults from the server.
          const q = await api(
            `/api/tenants/${encodeURIComponent(tenantId)}/quotas`,
          );
          populateQuotasForm(q);
          if (status) status.textContent = "Reset to defaults.";
        } catch (err) {
          if (status) status.textContent = `Failed: ${err.message}`;
        }
      };
    }
  }

  async function renderTenantUsage(tenantId, quotas) {
    const root = $("#tenant-usage");
    if (!root) return;
    let usage = null;
    try {
      usage = await api(`/api/tenants/${encodeURIComponent(tenantId)}/usage`);
    } catch (_) {
      root.innerHTML = `<p class="empty muted">Usage unavailable.</p>`;
      return;
    }
    const rows = [
      ["workspaces", usage.workspaces, quotas && quotas.max_workspaces],
      ["storage_gb", usage.storage_gb, quotas && quotas.max_storage_gb],
      ["active_tokens", usage.active_tokens, quotas && quotas.max_active_tokens],
      [
        "oauth_connections",
        usage.oauth_connections,
        quotas && quotas.max_oauth_connections,
      ],
      [
        "cost_usd_day",
        usage.cost_usd_day,
        quotas && quotas.max_cost_usd_day,
      ],
      [
        "cost_usd_month",
        usage.cost_usd_month,
        quotas && quotas.max_cost_usd_month,
      ],
    ];
    root.innerHTML = rows
      .map(([label, current, max]) => {
        const pct = max && max > 0 ? Math.min(100, (current / max) * 100) : 0;
        return `
          <div class="usage-row">
            <span class="usage-label">${escapeHtml(label)}</span>
            <span class="usage-meter" aria-hidden="true">
              <span class="usage-bar" style="width:${pct.toFixed(1)}%"></span>
            </span>
            <span class="usage-value mono">
              ${escapeHtml(String(current))} / ${escapeHtml(String(max ?? "—"))}
            </span>
          </div>
        `;
      })
      .join("");
    const note = (usage.notes && Object.keys(usage.notes).length)
      ? `<p class="muted">Some metrics live in other services; identity reports them as 0 with a notes map.</p>`
      : "";
    root.innerHTML += note;
  }

  async function renderTenantAudit(tenantId) {
    const body = $("#tenant-audit-body");
    if (!body) return;
    try {
      const audit = await api(
        `/api/audit?tenant_id=${encodeURIComponent(tenantId)}&limit=50`,
      );
      const events = audit.events || [];
      if (!events.length) {
        body.innerHTML = `<tr class="empty"><td colspan="5">No invocations yet.</td></tr>`;
        return;
      }
      body.innerHTML = events
        .map((e) => {
          return `
            <tr>
              <td class="time">${escapeHtml(fmtTime(e.timestamp))}</td>
              <td class="tool">${escapeHtml(e.tool_id || "?")}</td>
              <td>${e.cached ? "cached" : "fresh"}</td>
              <td class="num">${escapeHtml(fmtMs(e.duration_ms))}</td>
              <td class="num">${escapeHtml(fmtUSD(e.cost_estimate_usd))}</td>
            </tr>
          `;
        })
        .join("");
    } catch (_) {
      body.innerHTML = `<tr class="empty"><td colspan="5">Audit unavailable.</td></tr>`;
    }
  }

  // ---- v1.0: Channel schema evolution wizard --------------------------

  let schemaWizardCtx = { wsId: null, channel: null };

  function openSchemaWizard(wsId, channel, initialSchema) {
    schemaWizardCtx = { wsId, channel };
    const m = $("#schema-wizard-modal");
    if (!m) return;
    m.hidden = false;
    const title = $("#schema-wizard-title");
    if (title) title.textContent = `Edit schema — ${channel}`;
    const summary = $("#schema-wizard-summary");
    if (summary) {
      summary.textContent = `Workspace ${shortId(wsId, 14)}, channel ${channel}.`;
    }
    const editor = $("#schema-wizard-editor");
    if (editor) {
      editor.value = initialSchema
        ? JSON.stringify(initialSchema, null, 2)
        : "{\n  \"type\": \"object\"\n}\n";
      editor.oninput = updateSchemaWizardSyntax;
      updateSchemaWizardSyntax();
    }
    const result = $("#schema-check-result");
    if (result) result.innerHTML = "";
    const apply = $("#schema-apply-btn");
    if (apply) apply.disabled = true;
    wireSchemaWizardButtons();
  }

  function closeSchemaWizard() {
    const m = $("#schema-wizard-modal");
    if (m) m.hidden = true;
    schemaWizardCtx = { wsId: null, channel: null };
  }

  function readSchemaFromEditor() {
    const editor = $("#schema-wizard-editor");
    if (!editor) return null;
    try {
      return JSON.parse(editor.value);
    } catch (_) {
      return null;
    }
  }

  function updateSchemaWizardSyntax() {
    const status = $("#schema-wizard-syntax");
    if (!status) return;
    const parsed = readSchemaFromEditor();
    const apply = $("#schema-apply-btn");
    if (parsed === null) {
      status.textContent = "Invalid JSON.";
      status.className = "schema-status err";
      if (apply) apply.disabled = true;
      return;
    }
    status.textContent = "Valid JSON.";
    status.className = "schema-status ok";
    // Apply remains disabled until a green check.
  }

  function wireSchemaWizardButtons() {
    const closeBtn = $("#schema-wizard-close");
    if (closeBtn) closeBtn.onclick = closeSchemaWizard;

    const checkBtn = $("#schema-check-btn");
    if (checkBtn) checkBtn.onclick = doSchemaCheck;

    const applyBtn = $("#schema-apply-btn");
    if (applyBtn) applyBtn.onclick = doSchemaApply;

    const replayBtn = $("#schema-replay-all-btn");
    if (replayBtn) replayBtn.onclick = doDlqReplayDryRun;

    const purgeBtn = $("#schema-purge-btn");
    if (purgeBtn) purgeBtn.onclick = doDlqPurge;
  }

  async function doSchemaCheck() {
    const { wsId, channel } = schemaWizardCtx;
    const result = $("#schema-check-result");
    const apply = $("#schema-apply-btn");
    const schema = readSchemaFromEditor();
    if (!result) return;
    if (!schema) {
      result.innerHTML = `<p class="schema-status err">Fix the JSON syntax first.</p>`;
      return;
    }
    result.innerHTML = `<p class="muted">Checking&hellip;</p>`;
    try {
      const r = await fetch(
        `/api/workspaces/${encodeURIComponent(
          wsId,
        )}/channels/${encodeURIComponent(channel)}/schema/check`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ schema, scope: "both" }),
        },
      );
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const body = await r.json();
      const checked = body.checked || 0;
      const invalid = body.invalid || 0;
      const compatible = invalid === 0;
      const samples = (body.sample_failures || []).slice(0, 10);
      const samplesHtml = samples.length
        ? `<details>
             <summary>${samples.length} sample failure(s)</summary>
             <pre>${escapeHtml(JSON.stringify(samples, null, 2))}</pre>
           </details>`
        : "";
      result.innerHTML = `
        <p class="schema-status ${compatible ? "ok" : "err"}">
          ${checked} message(s) checked, ${invalid} invalid.
          ${compatible ? "Compatible." : "Incompatible."}
        </p>
        ${samplesHtml}
      `;
      if (apply) apply.disabled = !compatible;
    } catch (err) {
      result.innerHTML = `<p class="schema-status err">Failed: ${escapeHtml(
        err.message,
      )}</p>`;
    }
  }

  async function doSchemaApply() {
    const { wsId, channel } = schemaWizardCtx;
    const result = $("#schema-check-result");
    const schema = readSchemaFromEditor();
    if (!schema) return;
    try {
      const r = await fetch(
        `/api/workspaces/${encodeURIComponent(
          wsId,
        )}/channels/${encodeURIComponent(channel)}/schema`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ schema }),
        },
      );
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      if (result)
        result.innerHTML = `<p class="schema-status ok">Schema applied.</p>`;
    } catch (err) {
      if (result)
        result.innerHTML = `<p class="schema-status err">Apply failed: ${escapeHtml(
          err.message,
        )}</p>`;
    }
  }

  async function doDlqReplayDryRun() {
    const { wsId, channel } = schemaWizardCtx;
    const result = $("#schema-check-result");
    if (!result) return;
    try {
      const r = await fetch(
        `/api/workspaces/${encodeURIComponent(
          wsId,
        )}/channels/${encodeURIComponent(channel)}/deadletter/replay-all?dry_run=true`,
        { method: "POST" },
      );
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const body = await r.json();
      result.innerHTML = `
        <p class="schema-status">
          Dry run: would replay ${intFmt.format(body.attempted || 0)},
          succeed ${intFmt.format(body.succeeded || 0)},
          fail ${intFmt.format(body.failed || 0)}.
        </p>
      `;
    } catch (err) {
      result.innerHTML = `<p class="schema-status err">Replay dry-run failed: ${escapeHtml(
        err.message,
      )}</p>`;
    }
  }

  async function doDlqPurge() {
    const { wsId, channel } = schemaWizardCtx;
    const seconds = Number($("#schema-purge-seconds")?.value || "0");
    const result = $("#schema-check-result");
    if (!confirm(`Purge DLQ rows older than ${seconds}s?`)) return;
    try {
      const r = await fetch(
        `/api/workspaces/${encodeURIComponent(
          wsId,
        )}/channels/${encodeURIComponent(
          channel,
        )}/deadletter?older_than_seconds=${encodeURIComponent(seconds)}`,
        { method: "DELETE" },
      );
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const body = await r.json();
      if (result)
        result.innerHTML = `<p class="schema-status ok">Purged ${intFmt.format(
          body.purged || 0,
        )} row(s).</p>`;
    } catch (err) {
      if (result)
        result.innerHTML = `<p class="schema-status err">Purge failed: ${escapeHtml(
          err.message,
        )}</p>`;
    }
  }

  // Expose two helpers so existing channel rendering can call them:
  // ``window.PlinthSchemaWizard.open(wsId, channel, currentSchema?)``.
  window.PlinthSchemaWizard = {
    open: openSchemaWizard,
    close: closeSchemaWizard,
  };

  // ====================================================================
  // v1.5 — Workflow Replay (timeline scrubber + per-step state)
  // ====================================================================

  // Cached replay payload + scrub state. We hold the full timeline +
  // workflow rows in memory so dragging the scrubber doesn't refetch.
  const replayState = {
    wfId: null,
    wsId: null,
    workflow: null,
    timeline: [],
    snapshots: [],
    auditEvents: [],
    cursorTs: null,    // ISO string the user is currently scrubbed to
    rangeMin: null,    // earliest ts (workflow.created_at)
    rangeMax: null,    // latest ts (workflow.finished_at or NOW)
  };

  function _ts(s) {
    if (!s) return null;
    const t = new Date(s).getTime();
    return Number.isNaN(t) ? null : t;
  }

  async function loadWorkflowReplay() {
    const params = currentRoute.params || {};
    const wfId = params.wf_id;
    const wsId = params.ws_id;
    replayState.wfId = wfId;
    replayState.wsId = wsId;
    if (!wfId || !wsId) {
      const root = $("#replay-graph");
      if (root) {
        root.innerHTML = `<p class="empty muted">Missing ?ws=&lt;workspace_id&gt; in the URL.</p>`;
      }
      return;
    }
    setSpinner(true);
    try {
      const data = await api(
        `/api/workflows/${encodeURIComponent(wfId)}/replay?ws=${encodeURIComponent(
          wsId,
        )}`,
      );
      replayState.workflow = data.workflow || null;
      replayState.timeline = data.timeline || [];
      replayState.snapshots = data.snapshots || [];
      replayState.auditEvents = data.audit_events || [];

      const wf = replayState.workflow || {};
      const minMs = _ts(wf.created_at) ?? Date.now();
      const maxMs = _ts(wf.finished_at) ?? Date.now();
      replayState.rangeMin = minMs;
      replayState.rangeMax = Math.max(maxMs, minMs + 1);
      replayState.cursorTs = new Date(replayState.rangeMax).toISOString();

      renderReplayHead();
      renderReplaySvg();
      renderReplayGraph();
      renderReplayErrors();
      wireReplayControls();
      lastFetchAt = Date.now();
      tickRefreshLabel();
    } catch (err) {
      const root = $("#replay-graph");
      if (root) {
        root.innerHTML = `<p class="empty err">failed: ${escapeHtml(
          err.message,
        )}</p>`;
      }
    } finally {
      setSpinner(false);
    }
  }

  function renderReplayHead() {
    const wf = replayState.workflow;
    if (!wf) return;
    const nameEl = $("#replay-name");
    const idEl = $("#replay-id");
    const metaEl = $("#replay-meta");
    if (nameEl) nameEl.textContent = wf.name || wf.id || "Workflow replay";
    if (idEl) idEl.textContent = wf.id || "";
    if (metaEl) {
      const status = wf.status || "pending";
      metaEl.innerHTML = `
        <span class="workflow-status ${escapeHtml(status)}">${escapeHtml(status)}</span>
        <span class="muted">workspace ${escapeHtml(wf.workspace_id || "")}</span>
        <span class="muted">created ${escapeHtml(fmtDate(wf.created_at))}</span>
        <span class="muted">finished ${escapeHtml(
          wf.finished_at ? fmtDate(wf.finished_at) : "—",
        )}</span>
        <a class="btn small" href="#/workflows/${encodeURIComponent(
          wf.id || "",
        )}?ws=${encodeURIComponent(wf.workspace_id || "")}">live view</a>
      `;
    }
  }

  function renderReplaySvg() {
    const svg = document.getElementById("replay-svg");
    if (!svg) return;
    const wfTimeline = replayState.timeline || [];
    const minMs = replayState.rangeMin;
    const maxMs = replayState.rangeMax;
    const span = Math.max(1, maxMs - minMs);

    const W = 800;
    const H = 80;
    const padX = 12;
    const padY = 16;
    const innerW = W - padX * 2;
    const baseY = H - padY;

    let svgInner =
      `<line class="replay-axis" x1="${padX}" y1="${baseY}" ` +
      `x2="${W - padX}" y2="${baseY}" />`;

    // Time-axis labels (start, mid, end).
    const labels = [
      [padX, fmtTime(new Date(minMs).toISOString())],
      [padX + innerW / 2, fmtTime(new Date(minMs + span / 2).toISOString())],
      [W - padX, fmtTime(new Date(maxMs).toISOString())],
    ];
    for (const [x, txt] of labels) {
      svgInner +=
        `<text class="replay-tick-label" x="${x}" y="${H - 2}" ` +
        `text-anchor="middle">${escapeHtml(txt)}</text>`;
    }

    // Tick per timeline event. Colour-coded by event kind / status.
    for (const ev of wfTimeline) {
      const t = _ts(ev.ts);
      if (t == null) continue;
      const x = padX + ((t - minMs) / span) * innerW;
      let cls = "replay-tick";
      if (ev.kind === "step.finished") {
        cls += " replay-tick--" + (ev.status || "completed");
      } else if (ev.kind === "step.started") {
        cls += " replay-tick--running";
      }
      const top = ev.kind === "step.finished" ? padY : baseY - 30;
      const bot = baseY;
      svgInner +=
        `<line class="${cls}" x1="${x.toFixed(2)}" y1="${top}" ` +
        `x2="${x.toFixed(2)}" y2="${bot}">` +
        `<title>${escapeHtml(ev.kind)} ${escapeHtml(
          ev.step_name || "",
        )} @ ${escapeHtml(fmtDate(ev.ts))}</title></line>`;
    }

    // Cursor.
    const cursorMs = _ts(replayState.cursorTs) || maxMs;
    const cursorX = padX + ((cursorMs - minMs) / span) * innerW;
    svgInner +=
      `<line class="replay-cursor" x1="${cursorX.toFixed(2)}" y1="${padY - 4}" ` +
      `x2="${cursorX.toFixed(2)}" y2="${baseY + 4}" />`;

    svg.innerHTML = svgInner;

    // Sync the range slider position. The slider is normalised 0..1000 so
    // we don't have to rebind it whenever the workflow timespan changes.
    const range = document.getElementById("replay-range");
    if (range) {
      const norm = ((cursorMs - minMs) / span) * 1000;
      range.value = String(Math.round(Math.max(0, Math.min(1000, norm))));
    }
    const posEl = document.getElementById("replay-pos-time");
    if (posEl) posEl.textContent = fmtDate(replayState.cursorTs);
    const summary = document.getElementById("replay-scrub-summary");
    if (summary) {
      summary.textContent =
        `${wfTimeline.length} timeline events · drag to inspect past state`;
    }
  }

  function reconstructStateAt(cursorTs) {
    // Replay events up to the cursor and take the latest known state per
    // (step_name, attempt). This mirrors what the worker would have seen.
    const cursorMs = _ts(cursorTs) ?? Infinity;
    const byStep = new Map(); // step_name → { status, attempt, started_at, finished_at, error }
    for (const ev of replayState.timeline) {
      const t = _ts(ev.ts);
      if (t == null || t > cursorMs) continue;
      if (!ev.step_name) continue;
      const cur = byStep.get(ev.step_name) || {
        name: ev.step_name,
        status: "pending",
        attempt: ev.attempt || 1,
      };
      if (ev.kind === "step.created" && cur.status === "pending") {
        cur.status = "pending";
        cur.attempt = ev.attempt || cur.attempt;
      } else if (ev.kind === "step.started") {
        cur.status = "running";
        cur.started_at = ev.ts;
        cur.attempt = ev.attempt || cur.attempt;
      } else if (ev.kind === "step.finished") {
        cur.status = ev.status || "completed";
        cur.finished_at = ev.ts;
        cur.error = ev.error || null;
        cur.attempt = ev.attempt || cur.attempt;
      }
      byStep.set(ev.step_name, cur);
    }
    return byStep;
  }

  function renderReplayGraph() {
    const root = $("#replay-graph");
    if (!root) return;
    const wf = replayState.workflow;
    if (!wf) {
      root.innerHTML = `<p class="empty muted">No workflow loaded.</p>`;
      return;
    }
    const manifest = wf.steps_manifest || [];
    if (!manifest.length) {
      root.innerHTML = `<p class="empty muted">Workflow has no manifest entries.</p>`;
      return;
    }
    const stateByName = reconstructStateAt(replayState.cursorTs);

    const html = manifest
      .map((name, idx) => {
        const s = stateByName.get(name);
        const synthStep = s
          ? {
              status: s.status,
              attempt: s.attempt,
              started_at: s.started_at,
              finished_at: s.finished_at,
              error: s.error,
            }
          : null;
        const node = buildNodeHtml(name, synthStep);
        const edge =
          idx < manifest.length - 1
            ? `<div class="wf-edge" aria-hidden="true"></div>`
            : "";
        return `<div class="wf-graph-cell" data-key="${escapeHtml(
          nodeKey(name),
        )}">${node}${edge}</div>`;
      })
      .join("");
    root.innerHTML = html;

    const summary = $("#replay-graph-summary");
    if (summary) {
      const completed = Array.from(stateByName.values()).filter(
        (s) => s.status === "completed",
      ).length;
      summary.textContent =
        `${completed}/${manifest.length} steps complete @ ${fmtDate(
          replayState.cursorTs,
        )}`;
    }
  }

  function renderReplayErrors() {
    const root = $("#replay-errors");
    const summary = $("#replay-errors-summary");
    if (!root) return;
    const wf = replayState.workflow;
    if (!wf) {
      root.innerHTML = `<p class="empty muted">No workflow loaded.</p>`;
      return;
    }
    // Group every step row by step_name; show only steps that ended in
    // failure (or where the *latest* attempt is still failed).
    const byName = new Map();
    for (const s of wf.steps || []) {
      if (!byName.has(s.name)) byName.set(s.name, []);
      byName.get(s.name).push(s);
    }
    const failedNames = [];
    for (const [name, attempts] of byName.entries()) {
      const latest = attempts[attempts.length - 1];
      if (
        attempts.some((a) => a.status === "failed") ||
        (latest && latest.status === "failed")
      ) {
        failedNames.push(name);
      }
    }
    if (!failedNames.length) {
      root.innerHTML = `<p class="empty muted">No failures in this workflow.</p>`;
      if (summary) summary.textContent = "";
      return;
    }
    if (summary) {
      summary.textContent =
        failedNames.length +
        " step" +
        (failedNames.length === 1 ? "" : "s") +
        " failed at least once";
    }
    root.innerHTML = failedNames
      .map((name) => {
        const attempts = byName.get(name) || [];
        const failedAttempts = attempts.filter((a) => a.status === "failed");
        const latestFailed = failedAttempts[failedAttempts.length - 1] || null;
        const inputJson = latestFailed
          ? JSON.stringify(latestFailed.input || null, null, 2)
          : "";
        const attemptsList = attempts
          .map(
            (a) =>
              `<li>attempt ${escapeHtml(String(a.attempt))} — ${escapeHtml(
                a.status,
              )}${
                a.finished_at ? " @ " + fmtDate(a.finished_at) : ""
              }${
                a.error
                  ? ` <span class="muted">— ${escapeHtml(a.error)}</span>`
                  : ""
              }</li>`,
          )
          .join("");
        return `
          <div class="replay-error-row">
            <h3>${escapeHtml(name)} <span class="muted">— ${
          failedAttempts.length
        }/${attempts.length} attempt(s) failed</span></h3>
            ${
              latestFailed && latestFailed.error
                ? `<pre>${escapeHtml(latestFailed.error)}</pre>`
                : ""
            }
            ${
              inputJson && inputJson !== "null"
                ? `<details><summary class="muted">Last attempt input</summary><pre>${escapeHtml(
                    inputJson,
                  )}</pre></details>`
                : ""
            }
            <ul class="replay-error-attempts">${attemptsList}</ul>
          </div>
        `;
      })
      .join("");
  }

  function wireReplayControls() {
    const range = document.getElementById("replay-range");
    if (range) {
      range.oninput = () => {
        const norm = Number(range.value) / 1000;
        const minMs = replayState.rangeMin;
        const maxMs = replayState.rangeMax;
        const span = Math.max(1, maxMs - minMs);
        const t = minMs + norm * span;
        replayState.cursorTs = new Date(t).toISOString();
        renderReplaySvg();
        renderReplayGraph();
      };
    }
    const jump = document.getElementById("replay-jump-now");
    if (jump) {
      jump.onclick = () => {
        replayState.cursorTs = new Date(replayState.rangeMax).toISOString();
        renderReplaySvg();
        renderReplayGraph();
      };
    }
    const restore = document.getElementById("replay-restore-to-this");
    if (restore) {
      restore.onclick = () => {
        // Find the latest snapshot at-or-before the cursor.
        const cursorMs = _ts(replayState.cursorTs) || Date.now();
        const candidates = (replayState.snapshots || [])
          .filter((s) => {
            const t = _ts(s.created_at);
            return t != null && t <= cursorMs;
          })
          .sort((a, b) => _ts(b.created_at) - _ts(a.created_at));
        if (!candidates.length) {
          alert("No snapshot exists at or before this point.");
          return;
        }
        const snap = candidates[0];
        alert(
          `Latest snapshot at-or-before this point:\n\n` +
            `id:        ${snap.id}\n` +
            `name:      ${snap.name || "(unnamed)"}\n` +
            `created:   ${fmtDate(snap.created_at)}\n\n` +
            `Use the workspace API to restore this snapshot:\n` +
            `POST /v1/workspaces/${replayState.wsId}/snapshots/${snap.id}/restore`,
        );
      };
    }
  }

  // ====================================================================
  // v2 — Plinth Studio (visual workflow builder, drag-drop edition)
  // ====================================================================

  // Studio state. v2 supports proper HTML5 drag-and-drop: tiles from the
  // toolbox can be dropped on insertion zones between rows; rows can be
  // dragged to reorder; rows can be dragged onto the trash to delete (with
  // a 5s undo banner). The up/down/edit/delete row buttons remain as the
  // keyboard-accessible fallback.
  //
  // Every step is stamped with a stable string `__id` (in-memory only, not
  // persisted) so drag-and-drop can refer to rows regardless of their
  // current index.
  let studioIdCounter = 0;
  function nextStudioStepId() {
    studioIdCounter += 1;
    return "s" + studioIdCounter + "_" + Math.random().toString(36).slice(2, 8);
  }
  const STUDIO_TYPE_MIME = "application/x-plinth-step-type";
  const STUDIO_ID_MIME = "application/x-plinth-step-id";
  const STUDIO_UNDO_MS = 5000;

  const studioState = {
    workflow: {
      name: "",
      description: "",
      retry_policy: "exponential",
      max_attempts_default: 3,
      steps: [],
    },
    workspaces: [],
    selectedWsId: "",
    editingIndex: null,
    loadedFromId: null,
    // Drag-drop scratch state.
    draggingRowId: null,
    // Undo-banner state — when a row is deleted via trash or Delete-key.
    undo: null, // { step, index, timerId, deadline }
  };

  const STUDIO_STEP_DEFAULTS = {
    tool: { type: "tool", tool_id: "", arguments_template: {} },
    llm: {
      type: "llm",
      model: "claude-sonnet-4-5",
      system: "",
      prompt_template: "",
    },
    channel_send: { type: "channel_send", channel: "", payload_template: {} },
    channel_receive: { type: "channel_receive", channel: "" },
    manual: { type: "manual" },
  };

  async function loadStudio() {
    const sel = $("#studio-ws-select");
    const status = $("#studio-status");
    if (!sel) return;
    if (status) status.textContent = "loading workspaces…";
    try {
      const data = await api("/api/workspaces");
      studioState.workspaces = (data.workspaces || []).slice();
    } catch (err) {
      studioState.workspaces = [];
      if (status) {
        status.textContent = "failed to load workspaces: " + err.message;
        status.classList.add("err");
      }
    }
    sel.innerHTML =
      `<option value="">— choose —</option>` +
      studioState.workspaces
        .map(
          (w) =>
            `<option value="${escapeHtml(w.id)}">${escapeHtml(
              w.name || w.id,
            )}</option>`,
        )
        .join("");
    if (
      studioState.selectedWsId &&
      studioState.workspaces.some((w) => w.id === studioState.selectedWsId)
    ) {
      sel.value = studioState.selectedWsId;
    } else if (studioState.workspaces.length === 1) {
      // Single-workspace deployment: auto-pick.
      sel.value = studioState.workspaces[0].id;
      studioState.selectedWsId = studioState.workspaces[0].id;
    }
    if (status && !status.classList.contains("err")) status.textContent = "";

    renderStudioCanvas();
    wireStudioControls();
    populateStudioPropertiesForm();
  }

  function populateStudioPropertiesForm() {
    const form = $("#studio-properties-form");
    if (!form) return;
    if (form.elements.name) form.elements.name.value = studioState.workflow.name;
    if (form.elements.description)
      form.elements.description.value = studioState.workflow.description;
    if (form.elements.retry_policy)
      form.elements.retry_policy.value = studioState.workflow.retry_policy;
    if (form.elements.max_attempts_default)
      form.elements.max_attempts_default.value = String(
        studioState.workflow.max_attempts_default,
      );
  }

  function readStudioPropertiesForm() {
    const form = $("#studio-properties-form");
    if (!form) return;
    studioState.workflow.name = (form.elements.name?.value || "").trim();
    studioState.workflow.description = form.elements.description?.value || "";
    studioState.workflow.retry_policy =
      form.elements.retry_policy?.value || "exponential";
    studioState.workflow.max_attempts_default = Math.max(
      1,
      Number(form.elements.max_attempts_default?.value || 1),
    );
  }

  function studioStepLabel(step) {
    if (step.type === "tool") return step.tool_id || "(no tool)";
    if (step.type === "llm")
      return (step.model || "model?") + " · " + (step.system ? "sys" : "no sys");
    if (step.type === "channel_send")
      return "→ " + (step.channel || "channel?");
    if (step.type === "channel_receive")
      return "← " + (step.channel || "channel?");
    if (step.type === "manual") return "manual approval";
    return step.type || "(no type)";
  }

  function studioStepValid(step) {
    if (!step.name) return false;
    if (step.type === "tool" && !step.tool_id) return false;
    if (step.type === "llm" && (!step.model || !step.prompt_template)) return false;
    if (step.type === "channel_send" && !step.channel) return false;
    if (step.type === "channel_receive" && !step.channel) return false;
    return true;
  }

  // ---- helpers: identify and find steps regardless of index ----------

  function ensureStudioStepId(step) {
    if (step && !step.__id) step.__id = nextStudioStepId();
    return step;
  }

  function studioFindIndexById(id) {
    return studioState.workflow.steps.findIndex((s) => s && s.__id === id);
  }

  // Build a fresh step for a given type. Used by drag-drop insert + the
  // keyboard-fallback Enter handler.
  function makeStudioStep(type) {
    const defaults = STUDIO_STEP_DEFAULTS[type] || { type };
    const step = {
      name: `step_${studioState.workflow.steps.length + 1}`,
      ...JSON.parse(JSON.stringify(defaults)),
      max_attempts: studioState.workflow.max_attempts_default,
    };
    ensureStudioStepId(step);
    return step;
  }

  // ---- canvas render -------------------------------------------------

  function renderStudioCanvas() {
    const root = $("#studio-canvas");
    const summary = $("#studio-canvas-summary");
    if (!root) return;
    const steps = studioState.workflow.steps;
    steps.forEach(ensureStudioStepId);
    if (summary) {
      summary.textContent = steps.length
        ? `${steps.length} step${steps.length === 1 ? "" : "s"}`
        : "no steps yet";
    }

    // Empty state: still render a single drop zone (index 0) so the user
    // has somewhere to drop a tile. The visible empty-state placeholder
    // sits inside the zone.
    if (!steps.length) {
      root.innerHTML = `
        <li class="studio-zone studio-zone-only"
            data-index="0"
            aria-hidden="true">
          <div class="studio-zone-line"></div>
        </li>
        <li class="studio-canvas-empty muted" data-empty-state>
          Drop a step here to start.
        </li>`;
      wireStudioCanvasZones();
      return;
    }

    // Render: [zone 0][row 0][zone 1][row 1]…[zone N].
    const parts = [];
    parts.push(
      `<li class="studio-zone" data-index="0" aria-hidden="true">
         <div class="studio-zone-line"></div>
       </li>`,
    );
    steps.forEach((step, idx) => {
      const valid = studioStepValid(step);
      parts.push(`
          <li class="studio-step ${valid ? "" : "invalid"}"
              data-index="${idx}"
              data-step-id="${escapeHtml(step.__id)}"
              draggable="true"
              tabindex="0"
              role="listitem"
              aria-grabbed="false"
              aria-label="step ${idx + 1}: ${escapeHtml(step.name || "(unnamed)")}, type ${escapeHtml(step.type || "")}">
            <span class="studio-step-grip" aria-hidden="true">⋮⋮</span>
            <span class="studio-step-index">${idx + 1}</span>
            <div class="studio-step-body">
              <span class="studio-step-name">
                ${escapeHtml(step.name || "(unnamed)")}
                <span class="muted">— ${escapeHtml(step.type || "")}</span>
              </span>
              <span class="studio-step-meta" title="${escapeHtml(
                studioStepLabel(step),
              )}">${escapeHtml(studioStepLabel(step))}</span>
            </div>
            <span class="studio-step-actions">
              <button class="btn small" type="button" data-step-up
                ${idx === 0 ? "disabled" : ""}
                aria-label="move step up">↑</button>
              <button class="btn small" type="button" data-step-down
                ${idx === steps.length - 1 ? "disabled" : ""}
                aria-label="move step down">↓</button>
              <button class="btn small" type="button" data-step-edit
                aria-label="edit step">edit</button>
              <button class="btn small" type="button" data-step-remove
                aria-label="delete step">×</button>
            </span>
          </li>
        `);
      parts.push(
        `<li class="studio-zone" data-index="${idx + 1}" aria-hidden="true">
           <div class="studio-zone-line"></div>
         </li>`,
      );
    });
    root.innerHTML = parts.join("");
    wireStudioCanvasZones();
    wireStudioRowDrag();
  }

  // ---- drag-drop helpers --------------------------------------------

  function studioInsertNewStep(stepType, insertIndex) {
    if (!STUDIO_STEP_DEFAULTS[stepType] && stepType !== "manual") {
      // Unknown type → no-op. (Defends against weird MIME values.)
      return;
    }
    const step = makeStudioStep(stepType);
    const arr = studioState.workflow.steps;
    const idx = Math.max(0, Math.min(arr.length, insertIndex));
    arr.splice(idx, 0, step);
    renderStudioCanvas();
    // Open the editor immediately so the user can fill in required fields.
    openStudioStepEditor(idx);
  }

  function studioReorderStep(movingStepId, insertIndex) {
    const arr = studioState.workflow.steps;
    const from = studioFindIndexById(movingStepId);
    if (from < 0) return;
    // Convert insertIndex from "gap" semantics to a final array index.
    // Gap k means "before row k" in the BEFORE state. If we're moving a
    // row from `from` to gap `k`, the resulting index is k when k <= from,
    // otherwise k - 1 (because removing `from` shifts later gaps left by 1).
    let to = insertIndex;
    if (to > from) to -= 1;
    if (to === from) return; // No-op: dropped on its own slot.
    if (to < 0 || to > arr.length - 1) return;
    const [moved] = arr.splice(from, 1);
    arr.splice(to, 0, moved);
    renderStudioCanvas();
  }

  function studioDeleteStep(stepId, opts) {
    const arr = studioState.workflow.steps;
    const idx = studioFindIndexById(stepId);
    if (idx < 0) return;
    const [removed] = arr.splice(idx, 1);
    renderStudioCanvas();
    if (opts && opts.showUndo) {
      studioShowUndoBanner(removed, idx);
    }
  }

  // ---- undo banner ---------------------------------------------------

  function studioShowUndoBanner(step, index) {
    studioClearUndoBanner({ keepStateForRestore: false });
    const banner = $("#studio-undo-banner");
    const text = $("#studio-undo-text");
    const timer = $("#studio-undo-timer");
    const btn = $("#studio-undo-btn");
    if (!banner || !text || !timer || !btn) return;
    const label = step && step.name ? step.name : step && step.type ? step.type : "step";
    text.textContent = `Step "${label}" deleted.`;
    const deadline = Date.now() + STUDIO_UNDO_MS;
    const tick = () => {
      const remaining = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
      timer.textContent = `(${remaining}s)`;
      if (remaining <= 0) {
        studioClearUndoBanner({ keepStateForRestore: false });
      }
    };
    tick();
    const timerId = setInterval(tick, 250);
    studioState.undo = { step, index, timerId, deadline };
    banner.hidden = false;
    btn.onclick = studioUndoLastDelete;
  }

  function studioUndoLastDelete() {
    const u = studioState.undo;
    if (!u) return;
    const arr = studioState.workflow.steps;
    const idx = Math.max(0, Math.min(arr.length, u.index));
    arr.splice(idx, 0, u.step);
    studioClearUndoBanner({ keepStateForRestore: false });
    renderStudioCanvas();
  }

  function studioClearUndoBanner(opts) {
    const banner = $("#studio-undo-banner");
    if (studioState.undo) {
      if (studioState.undo.timerId) clearInterval(studioState.undo.timerId);
      if (!opts || !opts.keepStateForRestore) studioState.undo = null;
    }
    if (banner) banner.hidden = true;
  }

  // ---- canvas zones: drop-target wiring ------------------------------

  function wireStudioCanvasZones() {
    const root = $("#studio-canvas");
    if (!root) return;
    const zones = root.querySelectorAll(".studio-zone");
    zones.forEach((zone) => {
      zone.addEventListener("dragover", studioZoneDragOver);
      zone.addEventListener("dragleave", studioZoneDragLeave);
      zone.addEventListener("drop", studioZoneDrop);
    });
  }

  function studioZoneDragOver(ev) {
    // Accept either a fresh-tile insert or a row-reorder.
    const types = Array.from(ev.dataTransfer.types || []);
    const isInsert =
      types.includes(STUDIO_TYPE_MIME) ||
      document.body.classList.contains("studio-dragging-from-toolbox");
    const isReorder =
      types.includes(STUDIO_ID_MIME) ||
      document.body.classList.contains("studio-dragging-row");
    if (!isInsert && !isReorder) return;
    ev.preventDefault();
    ev.dataTransfer.dropEffect = isInsert ? "copy" : "move";
    ev.currentTarget.classList.add("studio-zone-active");
  }

  function studioZoneDragLeave(ev) {
    ev.currentTarget.classList.remove("studio-zone-active");
  }

  function studioZoneDrop(ev) {
    ev.preventDefault();
    const zone = ev.currentTarget;
    zone.classList.remove("studio-zone-active");
    const stepType = ev.dataTransfer.getData(STUDIO_TYPE_MIME);
    const movingId = ev.dataTransfer.getData(STUDIO_ID_MIME);
    const insertIndex = parseInt(zone.dataset.index, 10);
    if (Number.isNaN(insertIndex)) return;
    if (stepType) {
      studioInsertNewStep(stepType, insertIndex);
    } else if (movingId) {
      studioReorderStep(movingId, insertIndex);
    }
    document.body.classList.remove("studio-dragging-from-toolbox");
    document.body.classList.remove("studio-dragging-row");
    studioState.draggingRowId = null;
  }

  // ---- row dragging --------------------------------------------------

  function wireStudioRowDrag() {
    const root = $("#studio-canvas");
    if (!root) return;
    const rows = root.querySelectorAll(".studio-step");
    rows.forEach((row) => {
      row.addEventListener("dragstart", studioRowDragStart);
      row.addEventListener("dragend", studioRowDragEnd);
    });
  }

  function studioRowDragStart(ev) {
    const row = ev.currentTarget;
    const id = row.dataset.stepId;
    if (!id) return;
    try {
      ev.dataTransfer.setData(STUDIO_ID_MIME, id);
      // Some browsers (Firefox) refuse drag without text/plain payload.
      ev.dataTransfer.setData("text/plain", id);
      ev.dataTransfer.effectAllowed = "move";
    } catch (_err) {
      // Old Safari sometimes throws on setData with custom MIME; degrade.
    }
    row.classList.add("studio-row-dragging");
    row.setAttribute("aria-grabbed", "true");
    document.body.classList.add("studio-dragging-row");
    studioState.draggingRowId = id;
  }

  function studioRowDragEnd(ev) {
    const row = ev.currentTarget;
    row.classList.remove("studio-row-dragging");
    row.setAttribute("aria-grabbed", "false");
    document.body.classList.remove("studio-dragging-row");
    studioState.draggingRowId = null;
    // Clean up any zone that might have kept its "active" class on
    // browsers that swallow dragleave on drop-cancel.
    $$(".studio-zone-active").forEach((z) =>
      z.classList.remove("studio-zone-active"),
    );
  }

  // ---- toolbox tiles -------------------------------------------------

  function wireStudioToolboxTiles() {
    $$(".studio-tool").forEach((tile) => {
      tile.addEventListener("dragstart", studioTileDragStart);
      tile.addEventListener("dragend", studioTileDragEnd);
      // Keyboard fallback: click / Enter appends at end (v1.5 parity).
      tile.onclick = () => {
        const t = tile.dataset.stepType;
        if (!t) return;
        studioInsertNewStep(t, studioState.workflow.steps.length);
      };
    });
  }

  function studioTileDragStart(ev) {
    const tile = ev.currentTarget;
    const t = tile.dataset.stepType;
    if (!t) return;
    try {
      ev.dataTransfer.setData(STUDIO_TYPE_MIME, t);
      // Firefox requires a non-empty text/* payload to actually start a drag.
      ev.dataTransfer.setData("text/plain", t);
      ev.dataTransfer.effectAllowed = "copy";
    } catch (_err) {
      // ignore
    }
    tile.classList.add("studio-tool-dragging");
    tile.setAttribute("aria-grabbed", "true");
    document.body.classList.add("studio-dragging-from-toolbox");
  }

  function studioTileDragEnd(ev) {
    const tile = ev.currentTarget;
    tile.classList.remove("studio-tool-dragging");
    tile.setAttribute("aria-grabbed", "false");
    document.body.classList.remove("studio-dragging-from-toolbox");
    $$(".studio-zone-active").forEach((z) =>
      z.classList.remove("studio-zone-active"),
    );
  }

  // ---- trash zone ----------------------------------------------------

  function wireStudioTrash() {
    const trash = $("#studio-trash");
    if (!trash) return;
    trash.addEventListener("dragover", (ev) => {
      const types = Array.from(ev.dataTransfer.types || []);
      if (
        !types.includes(STUDIO_ID_MIME) &&
        !document.body.classList.contains("studio-dragging-row")
      ) {
        return;
      }
      ev.preventDefault();
      ev.dataTransfer.dropEffect = "move";
      trash.classList.add("studio-trash-active");
    });
    trash.addEventListener("dragleave", () => {
      trash.classList.remove("studio-trash-active");
    });
    trash.addEventListener("drop", (ev) => {
      ev.preventDefault();
      trash.classList.remove("studio-trash-active");
      const stepId =
        ev.dataTransfer.getData(STUDIO_ID_MIME) || studioState.draggingRowId;
      if (stepId) studioDeleteStep(stepId, { showUndo: true });
      document.body.classList.remove("studio-dragging-row");
      studioState.draggingRowId = null;
    });
    // Keyboard: activating the trash button doesn't make sense without a
    // selected row, so ignore Enter/Space here. The Delete-on-row binding
    // is the keyboard path.
  }

  // ---- canvas row interactions (click + keyboard) --------------------

  function wireStudioCanvasInteractions() {
    const canvas = $("#studio-canvas");
    if (!canvas) return;
    canvas.onclick = (ev) => {
      const target = ev.target;
      if (!(target instanceof HTMLElement)) return;
      const li = target.closest(".studio-step");
      if (!li) return;
      const idx = Number(li.dataset.index);
      if (Number.isNaN(idx)) return;
      if (target.matches("[data-step-up]")) {
        if (idx > 0) {
          const arr = studioState.workflow.steps;
          [arr[idx - 1], arr[idx]] = [arr[idx], arr[idx - 1]];
          renderStudioCanvas();
        }
      } else if (target.matches("[data-step-down]")) {
        const arr = studioState.workflow.steps;
        if (idx < arr.length - 1) {
          [arr[idx], arr[idx + 1]] = [arr[idx + 1], arr[idx]];
          renderStudioCanvas();
        }
      } else if (target.matches("[data-step-remove]")) {
        const id = li.dataset.stepId;
        if (id) studioDeleteStep(id, { showUndo: true });
      } else if (target.matches("[data-step-edit]")) {
        openStudioStepEditor(idx);
      }
    };
    canvas.onkeydown = (ev) => {
      const target = ev.target;
      if (!(target instanceof HTMLElement)) return;
      const li = target.closest(".studio-step");
      if (!li) return;
      if (ev.key === "Delete" || ev.key === "Backspace") {
        ev.preventDefault();
        const id = li.dataset.stepId;
        if (id) studioDeleteStep(id, { showUndo: true });
      } else if (ev.key === "Enter") {
        // Enter on the row (not on a child button) opens the editor.
        if (target === li) {
          ev.preventDefault();
          openStudioStepEditor(Number(li.dataset.index));
        }
      }
    };
  }

  function wireStudioControls() {
    wireStudioToolboxTiles();
    wireStudioCanvasInteractions();
    wireStudioTrash();

    // Workspace picker.
    const sel = $("#studio-ws-select");
    if (sel) {
      sel.onchange = () => {
        studioState.selectedWsId = sel.value || "";
      };
    }

    // Save button.
    const save = $("#studio-save");
    if (save) save.onclick = saveStudioWorkflow;

    // Export button.
    const exp = $("#studio-export");
    if (exp) exp.onclick = exportStudioWorkflow;

    // Load button.
    const load = $("#studio-load");
    if (load) load.onclick = openStudioLoadModal;

    // Properties form: live-sync to state on input.
    const form = $("#studio-properties-form");
    if (form) {
      form.oninput = readStudioPropertiesForm;
    }

    // Step modal: close + submit.
    const closeBtn = $("#studio-step-modal-close");
    if (closeBtn) closeBtn.onclick = closeStudioStepEditor;
    const stepForm = $("#studio-step-form");
    if (stepForm) stepForm.onsubmit = submitStudioStepEditor;
    const delBtn = $("#studio-step-delete");
    if (delBtn) delBtn.onclick = deleteStudioStepFromEditor;
  }

  function openStudioStepEditor(idx) {
    const modal = $("#studio-step-modal");
    if (!modal) return;
    const step = studioState.workflow.steps[idx];
    if (!step) return;
    studioState.editingIndex = idx;
    const title = $("#studio-step-modal-title");
    if (title) title.textContent = `Edit step #${idx + 1}`;

    const form = $("#studio-step-form");
    if (form) {
      form.elements.index.value = String(idx);
      form.elements.type.value = step.type || "";
      form.elements.name.value = step.name || "";
    }

    const fields = $("#studio-step-fields");
    if (fields) {
      fields.innerHTML = renderStudioStepFields(step);
    }
    const status = $("#studio-step-status");
    if (status) status.textContent = "";
    modal.hidden = false;
  }

  function renderStudioStepFields(step) {
    if (step.type === "tool") {
      const args = JSON.stringify(step.arguments_template || {}, null, 2);
      return `
        <label class="qf-row">
          <span>tool_id</span>
          <input type="text" name="tool_id" placeholder="web.search"
                 value="${escapeHtml(step.tool_id || "")}" required />
        </label>
        <label class="qf-row">
          <span>arguments_template (JSON)</span>
          <textarea name="arguments_template" rows="6">${escapeHtml(args)}</textarea>
        </label>
        <label class="qf-row">
          <span>max_attempts</span>
          <input type="number" min="1" max="100" name="max_attempts"
                 value="${escapeHtml(String(step.max_attempts || 1))}" />
        </label>
      `;
    }
    if (step.type === "llm") {
      return `
        <label class="qf-row">
          <span>model</span>
          <input type="text" name="model" placeholder="claude-sonnet-4-5"
                 value="${escapeHtml(step.model || "")}" required />
        </label>
        <label class="qf-row">
          <span>system</span>
          <textarea name="system" rows="3">${escapeHtml(step.system || "")}</textarea>
        </label>
        <label class="qf-row">
          <span>prompt_template</span>
          <textarea name="prompt_template" rows="6" required>${escapeHtml(
            step.prompt_template || "",
          )}</textarea>
        </label>
        <label class="qf-row">
          <span>max_attempts</span>
          <input type="number" min="1" max="100" name="max_attempts"
                 value="${escapeHtml(String(step.max_attempts || 1))}" />
        </label>
      `;
    }
    if (step.type === "channel_send" || step.type === "channel_receive") {
      const isSend = step.type === "channel_send";
      const payload = JSON.stringify(step.payload_template || {}, null, 2);
      return `
        <label class="qf-row">
          <span>channel</span>
          <input type="text" name="channel" placeholder="out"
                 value="${escapeHtml(step.channel || "")}" required />
        </label>
        ${
          isSend
            ? `<label class="qf-row">
                <span>payload_template (JSON)</span>
                <textarea name="payload_template" rows="6">${escapeHtml(
                  payload,
                )}</textarea>
              </label>`
            : ""
        }
        <label class="qf-row">
          <span>max_attempts</span>
          <input type="number" min="1" max="100" name="max_attempts"
                 value="${escapeHtml(String(step.max_attempts || 1))}" />
        </label>
      `;
    }
    if (step.type === "manual") {
      return `<p class="muted">Manual approval — placeholder for human-in-loop. No config required.</p>`;
    }
    return `<p class="muted">Unknown step type: ${escapeHtml(step.type || "")}</p>`;
  }

  function closeStudioStepEditor() {
    const modal = $("#studio-step-modal");
    if (modal) modal.hidden = true;
    studioState.editingIndex = null;
  }

  function submitStudioStepEditor(ev) {
    ev.preventDefault();
    const form = ev.target;
    const idx = Number(form.elements.index.value);
    const step = studioState.workflow.steps[idx];
    if (!step) {
      closeStudioStepEditor();
      return;
    }
    step.name = (form.elements.name.value || "").trim();
    if (form.elements.tool_id) step.tool_id = form.elements.tool_id.value || "";
    if (form.elements.model) step.model = form.elements.model.value || "";
    if (form.elements.system) step.system = form.elements.system.value || "";
    if (form.elements.prompt_template)
      step.prompt_template = form.elements.prompt_template.value || "";
    if (form.elements.channel) step.channel = form.elements.channel.value || "";
    if (form.elements.max_attempts) {
      step.max_attempts = Math.max(
        1,
        Number(form.elements.max_attempts.value || 1),
      );
    }
    if (form.elements.arguments_template) {
      try {
        step.arguments_template = JSON.parse(
          form.elements.arguments_template.value || "{}",
        );
      } catch (err) {
        const status = $("#studio-step-status");
        if (status) {
          status.textContent = "arguments_template must be JSON: " + err.message;
          status.style.color = "var(--red, #b91c1c)";
        }
        return;
      }
    }
    if (form.elements.payload_template) {
      try {
        step.payload_template = JSON.parse(
          form.elements.payload_template.value || "{}",
        );
      } catch (err) {
        const status = $("#studio-step-status");
        if (status) {
          status.textContent = "payload_template must be JSON: " + err.message;
          status.style.color = "var(--red, #b91c1c)";
        }
        return;
      }
    }
    closeStudioStepEditor();
    renderStudioCanvas();
  }

  function deleteStudioStepFromEditor() {
    const idx = studioState.editingIndex;
    if (idx == null) return;
    const step = studioState.workflow.steps[idx];
    const id = step && step.__id;
    closeStudioStepEditor();
    if (id) {
      studioDeleteStep(id, { showUndo: true });
    } else {
      // Fallback: no id (shouldn't happen post-v2) — splice + re-render.
      studioState.workflow.steps.splice(idx, 1);
      renderStudioCanvas();
    }
  }

  function studioWorkflowToDefinition() {
    readStudioPropertiesForm();
    const wf = studioState.workflow;
    // Strip the in-memory `__id` so the persisted definition stays clean.
    const steps = wf.steps.map((s) => {
      const out = Object.assign({}, s);
      delete out.__id;
      return out;
    });
    return {
      name: wf.name,
      description: wf.description || "",
      retry_policy: wf.retry_policy,
      max_attempts_default: wf.max_attempts_default,
      steps,
    };
  }

  async function saveStudioWorkflow() {
    const status = $("#studio-status");
    const setStatus = (text, kind) => {
      if (!status) return;
      status.textContent = text;
      status.classList.remove("ok", "err");
      if (kind) status.classList.add(kind);
    };

    if (!studioState.selectedWsId) {
      setStatus("Choose a workspace to save into.", "err");
      return;
    }
    const def = studioWorkflowToDefinition();
    if (!def.name) {
      setStatus("Workflow name is required.", "err");
      return;
    }
    if (!def.steps.length) {
      setStatus("Add at least one step.", "err");
      return;
    }
    const invalid = def.steps.find((s) => !studioStepValid(s));
    if (invalid) {
      setStatus(`Step "${invalid.name}" is missing required fields.`, "err");
      return;
    }
    setStatus("saving…");

    try {
      const r = await fetch(
        `/api/workspaces/${encodeURIComponent(studioState.selectedWsId)}/workflows/import`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Accept: "application/json",
          },
          body: JSON.stringify(def),
        },
      );
      const body = await r.json().catch(() => ({}));
      if (!r.ok) {
        const msg = (body && body.error && body.error.message) || `HTTP ${r.status}`;
        setStatus("save failed: " + msg, "err");
        return;
      }
      setStatus(`saved · workflow ${body.id}`, "ok");
      // Redirect to replay page so the user can see (or run) the new workflow.
      location.hash = `#/workflows/${encodeURIComponent(
        body.id,
      )}/replay?ws=${encodeURIComponent(studioState.selectedWsId)}`;
    } catch (err) {
      setStatus("save failed: " + err.message, "err");
    }
  }

  function exportStudioWorkflow() {
    const def = studioWorkflowToDefinition();
    const blob = new Blob([JSON.stringify(def, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = (def.name || "workflow") + ".plinth.json";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  async function openStudioLoadModal() {
    const wsId = studioState.selectedWsId;
    const status = $("#studio-status");
    if (!wsId) {
      if (status) {
        status.textContent = "Choose a workspace first.";
        status.classList.add("err");
      }
      return;
    }
    let workflows = [];
    try {
      const data = await api(
        `/api/workspaces/${encodeURIComponent(wsId)}/workflows`,
      );
      workflows = data.workflows || [];
    } catch (err) {
      if (status) {
        status.textContent = "list failed: " + err.message;
        status.classList.add("err");
      }
      return;
    }
    if (!workflows.length) {
      alert("No workflows in this workspace yet. Save one first.");
      return;
    }
    const labels = workflows.map((w) => `${w.id} — ${w.name}`).join("\n");
    const choice = prompt(
      "Enter workflow id to load:\n\n" + labels,
      workflows[0].id,
    );
    if (!choice) return;
    const wf = workflows.find((w) => w.id === choice.trim());
    if (!wf) {
      if (status) {
        status.textContent = "no workflow with id " + choice;
        status.classList.add("err");
      }
      return;
    }
    const md = wf.metadata || {};
    if (md.definition && Array.isArray(md.definition.steps)) {
      // Imported workflow with full definition: load it back wholesale.
      studioState.workflow = {
        name: md.definition.name || wf.name || "",
        description: md.definition.description || "",
        retry_policy: md.definition.retry_policy || "exponential",
        max_attempts_default: md.definition.max_attempts_default || 3,
        steps: md.definition.steps.slice(),
      };
    } else {
      // Legacy workflow (no definition): synthesise a manual-step skeleton
      // from the manifest so the user can re-edit.
      studioState.workflow = {
        name: wf.name,
        description: "",
        retry_policy: "exponential",
        max_attempts_default: 3,
        steps: (wf.steps_manifest || []).map((n) => ({
          name: n,
          type: "manual",
          max_attempts: 1,
        })),
      };
    }
    // Stamp every loaded step with a fresh in-memory id for drag-drop.
    studioState.workflow.steps.forEach(ensureStudioStepId);
    studioState.loadedFromId = wf.id;
    if (status) {
      status.textContent = "loaded " + wf.id;
      status.classList.remove("err");
      status.classList.add("ok");
    }
    populateStudioPropertiesForm();
    renderStudioCanvas();
  }

  // Expose a tiny surface for tests / power-users. v2 adds drag-drop
  // helpers (insertNew / reorder / delete / undo) so callers can simulate
  // drops without dispatching synthetic DragEvents (which is fiddly across
  // browser engines).
  window.PlinthStudio = {
    state: studioState,
    canvasToDefinition: studioWorkflowToDefinition,
    save: saveStudioWorkflow,
    insertNewStep: studioInsertNewStep,
    reorderStep: studioReorderStep,
    deleteStep: studioDeleteStep,
    undoLastDelete: studioUndoLastDelete,
    render: renderStudioCanvas,
  };
  window.PlinthReplay = {
    state: replayState,
    reconstructStateAt,
  };
})();
