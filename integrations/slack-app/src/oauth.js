// SPDX-License-Identifier: Apache-2.0
// Standalone "Add to Slack" OAuth handler.
//
// Run this alongside (or in front of) the main bolt worker. It exposes
// two HTTP endpoints:
//
//   GET /slack/install   - 302 redirects the user into Slack's OAuth
//                          consent screen
//   GET /slack/oauth/callback
//                        - Slack redirects here after the user clicks
//                          "Allow". We exchange the temporary code for a
//                          real bot token + signing secret, persist them
//                          to ${INSTALLATIONS_PATH}, then show a small
//                          success page.
//
// In production you'd persist tokens in Postgres / Redis. For the MVP
// this writes JSONL — same approach as the savings sink — so the
// out-of-the-box install path doesn't drag in a DB.

import 'dotenv/config';
import { createServer } from 'node:http';
import { writeFile, readFile, mkdir } from 'node:fs/promises';
import { dirname } from 'node:path';
import { URL } from 'node:url';
import crypto from 'node:crypto';

const cfg = {
  clientId: req('SLACK_CLIENT_ID'),
  clientSecret: req('SLACK_CLIENT_SECRET'),
  publicUrl: req('PUBLIC_URL'),   // e.g. https://slack-install.plynf.com
  installationsPath: process.env.INSTALLATIONS_PATH || './data/installations.jsonl',
  // Comma-separated scope list. Pulled from manifest.yaml so the consent
  // screen matches what the bot actually needs.
  scopes:
    process.env.SLACK_SCOPES ||
    'app_mentions:read,chat:write,commands,im:history,im:write,assistant:write',
  port: parseInt(process.env.PORT || '4000', 10),
};

function req(name) {
  const v = process.env[name];
  if (!v) { console.error(`Missing required env var: ${name}`); process.exit(1); }
  return v;
}

// In-memory cache of "state" tokens we issued, so we can reject replays.
const issuedStates = new Map();
const STATE_TTL_MS = 10 * 60 * 1000;

function issueState() {
  const s = crypto.randomBytes(24).toString('hex');
  issuedStates.set(s, Date.now() + STATE_TTL_MS);
  return s;
}

function consumeState(s) {
  const expires = issuedStates.get(s);
  if (!expires) return false;
  issuedStates.delete(s);
  return expires > Date.now();
}

async function persistInstallation(record) {
  await mkdir(dirname(cfg.installationsPath), { recursive: true });
  const line = JSON.stringify({ ...record, installed_at: new Date().toISOString() });
  await writeFile(cfg.installationsPath, line + '\n', { flag: 'a' });
}

async function handleInstall(req, res) {
  const state = issueState();
  const params = new URLSearchParams({
    client_id: cfg.clientId,
    scope: cfg.scopes,
    user_scope: '',
    redirect_uri: `${cfg.publicUrl.replace(/\/+$/, '')}/slack/oauth/callback`,
    state,
  });
  res.writeHead(302, {
    Location: `https://slack.com/oauth/v2/authorize?${params}`,
  });
  res.end();
}

async function handleCallback(req, res) {
  const url = new URL(req.url, cfg.publicUrl);
  const code = url.searchParams.get('code');
  const state = url.searchParams.get('state');
  const error = url.searchParams.get('error');

  if (error) return sendError(res, 400, `Slack denied: ${error}`);
  if (!code) return sendError(res, 400, 'Missing code parameter.');
  if (!state || !consumeState(state)) {
    return sendError(res, 400, 'Invalid or expired state — please restart the install.');
  }

  const tokenUrl = 'https://slack.com/api/oauth.v2.access';
  const body = new URLSearchParams({
    client_id: cfg.clientId,
    client_secret: cfg.clientSecret,
    code,
    redirect_uri: `${cfg.publicUrl.replace(/\/+$/, '')}/slack/oauth/callback`,
  });

  const slack = await fetch(tokenUrl, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body,
  }).then((r) => r.json());

  if (!slack.ok) {
    return sendError(res, 400, `Slack returned not-ok: ${slack.error ?? 'unknown'}`);
  }

  // Persist what we need to run the bot for this workspace.
  await persistInstallation({
    team_id: slack.team?.id,
    team_name: slack.team?.name,
    bot_user_id: slack.bot_user_id,
    bot_token: slack.access_token,
    scope: slack.scope,
    enterprise_id: slack.enterprise?.id ?? null,
  });

  res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
  res.end(`<!doctype html>
<html><head><meta charset="utf-8"><title>Plynf · Installed</title>
<style>
body{font-family:system-ui,sans-serif;max-width:560px;margin:8vh auto;padding:1rem;color:#222}
.card{border:1px solid #e2e2e2;border-radius:12px;padding:2rem;box-shadow:0 4px 12px rgba(0,0,0,.04)}
h1{margin-top:0}
code{background:#f4f4f6;padding:.1em .3em;border-radius:4px}
.ok{color:#16a34a;font-size:3rem;line-height:1}
</style></head><body>
<div class="card">
  <div class="ok">✓</div>
  <h1>Plynf is installed in ${escapeHtml(slack.team?.name ?? 'your workspace')}</h1>
  <p>Try it now in Slack: <code>@Plynf what is the status of order #12345?</code> or
     run <code>/plynf-fetch get_order {"order_id":"12345"}</code>.</p>
  <p>Token-savings appear in your Plynf dashboard within 30 s of the first call.</p>
  <p style="color:#888;font-size:.875rem">You can close this tab.</p>
</div></body></html>`);
}

function sendError(res, status, msg) {
  res.writeHead(status, { 'Content-Type': 'text/plain; charset=utf-8' });
  res.end(`Plynf install error: ${msg}`);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

const server = createServer(async (req, res) => {
  try {
    if (req.url === '/slack/install') return await handleInstall(req, res);
    if (req.url?.startsWith('/slack/oauth/callback')) return await handleCallback(req, res);
    if (req.url === '/healthz') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      return res.end('{"status":"ok"}');
    }
    res.writeHead(404, { 'Content-Type': 'text/plain' });
    res.end('Not found');
  } catch (e) {
    console.error('OAuth handler error', e);
    sendError(res, 500, e.message);
  }
});

server.listen(cfg.port, () => {
  console.log(`⚡ Plynf Slack OAuth handler up on :${cfg.port}`);
  console.log(`  Install URL: ${cfg.publicUrl}/slack/install`);
});
