// Plynf OAuth Broker.
//
// Stateless proxy that handles the OAuth dance with three pre-registered
// providers (GitHub, Linear, Notion) so that local Plynf installs don't
// have to register their own OAuth apps. PKCE everywhere — no client
// secrets needed for GitHub or Linear; Notion uses confidential client
// where the secret stays here.
//
// Endpoints:
//   POST /v1/oauth/start
//     Body: { provider, local_callback, scopes?, state? }
//     Response: { authorize_url, state }
//     Persists the local_callback under a generated state token for ≤15 min.
//
//   GET /v1/oauth/cb?code=...&state=...
//     Called by the provider. Looks up the stored local_callback, exchanges
//     the code for a token by hitting the provider's token endpoint, then
//     302-redirects to local_callback with the token in the URL fragment
//     (so it never lands in server logs / Referer headers).
//
//   GET /health
//     Returns { ok: true } so uptime checks work.
//
// CRITICAL: tokens are NEVER persisted broker-side. The exchange is in
// memory, the redirect carries the token, the local Plynf identity service
// stores it encrypted at rest. Test test/test_no_persistence.ts asserts
// no token-shaped string ever lands in KV.

import { Hono } from "hono";
import { cors } from "hono/cors";
import { logger } from "hono/logger";

import type { Provider } from "./providers";
import { providers } from "./providers";

type Bindings = {
  STATE: KVNamespace;
  GITHUB_CLIENT_ID: string;
  LINEAR_CLIENT_ID: string;
  NOTION_CLIENT_ID: string;
  NOTION_CLIENT_SECRET?: string;
  BROKER_CALLBACK_BASE: string;
};

const app = new Hono<{ Bindings: Bindings }>();

app.use("*", logger());
app.use("/v1/*", cors({
  // Allow loopback from any port (Plynf default is 7425).
  origin: (origin) =>
    /^https?:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(origin || "") ? origin : null,
  allowMethods: ["GET", "POST", "OPTIONS"],
  allowHeaders: ["Content-Type"],
  maxAge: 300,
}));

// ─── Health ───────────────────────────────────────────────────────────

app.get("/health", (c) => c.json({ ok: true, version: "0.1.0" }));

// ─── /v1/oauth/start ──────────────────────────────────────────────────

app.post("/v1/oauth/start", async (c) => {
  const body = await c.req.json<{
    provider: string;
    local_callback: string;
    scopes?: string;
    state?: string;
  }>();

  if (!body.provider || !body.local_callback) {
    return c.json({ error: "provider and local_callback are required" }, 400);
  }

  const provider = providers[body.provider as keyof typeof providers];
  if (!provider) {
    return c.json({ error: `unknown provider: ${body.provider}` }, 404);
  }

  // Validate the local_callback is a loopback URL — prevents SSRF abuse.
  if (!isLoopbackUrl(body.local_callback)) {
    return c.json({ error: "local_callback must be a 127.0.0.1 or localhost URL" }, 400);
  }

  // Generate state token + PKCE verifier
  const state = await randomToken(16);
  const verifier = await randomToken(32);
  const challenge = await pkceChallenge(verifier);

  // Persist for 15 min
  await c.env.STATE.put(
    `state:${state}`,
    JSON.stringify({
      provider: provider.id,
      local_callback: body.local_callback,
      verifier,
      original_state: body.state ?? null,
      created_at: Date.now(),
    }),
    { expirationTtl: 900 },
  );

  const clientId = pickClientId(provider, c.env);
  const authorizeUrl = buildAuthorizeUrl({
    provider,
    clientId,
    challenge,
    state,
    redirectUri: `${c.env.BROKER_CALLBACK_BASE}/v1/oauth/cb`,
    scopes: body.scopes ?? provider.defaultScopes,
  });

  return c.json({ authorize_url: authorizeUrl, state });
});

// ─── /v1/oauth/cb ─────────────────────────────────────────────────────

app.get("/v1/oauth/cb", async (c) => {
  const code = c.req.query("code");
  const state = c.req.query("state");
  const errorParam = c.req.query("error");

  if (errorParam) {
    return c.text(`OAuth error: ${errorParam}`, 400);
  }
  if (!code || !state) {
    return c.text("missing code or state", 400);
  }

  const raw = await c.env.STATE.get(`state:${state}`);
  if (!raw) {
    return c.text("state expired or invalid — restart the OAuth flow", 410);
  }
  const stored = JSON.parse(raw) as {
    provider: string;
    local_callback: string;
    verifier: string;
    original_state: string | null;
  };

  // Single-use — burn the state immediately.
  await c.env.STATE.delete(`state:${state}`);

  const provider = providers[stored.provider as keyof typeof providers];
  if (!provider) {
    return c.text(`unknown provider in state: ${stored.provider}`, 500);
  }

  // Exchange code for token (provider-specific HTTP call)
  const clientId = pickClientId(provider, c.env);
  const clientSecret = provider.confidential ? pickClientSecret(provider, c.env) : undefined;

  let tokenJson: Record<string, unknown>;
  try {
    tokenJson = await exchangeToken({
      provider,
      code,
      verifier: stored.verifier,
      clientId,
      clientSecret,
      redirectUri: `${c.env.BROKER_CALLBACK_BASE}/v1/oauth/cb`,
    });
  } catch (e) {
    return c.text(`token exchange failed: ${(e as Error).message}`, 502);
  }

  // Send token to local callback in URL fragment (not query — fragments
  // are not sent in Referer headers and not logged by most proxies).
  const fragmentParts = new URLSearchParams({
    access_token: String(tokenJson.access_token ?? ""),
    token_type: String(tokenJson.token_type ?? "Bearer"),
    state: stored.original_state ?? "",
  });
  if (tokenJson.refresh_token) {
    fragmentParts.set("refresh_token", String(tokenJson.refresh_token));
  }
  if (tokenJson.expires_in) {
    fragmentParts.set("expires_in", String(tokenJson.expires_in));
  }
  if (tokenJson.scope) {
    fragmentParts.set("scope", String(tokenJson.scope));
  }

  const redirectTarget = `${stored.local_callback}#${fragmentParts.toString()}`;
  return c.redirect(redirectTarget, 302);
});

// ─── Helpers ──────────────────────────────────────────────────────────

function isLoopbackUrl(url: string): boolean {
  try {
    const u = new URL(url);
    return u.hostname === "127.0.0.1" || u.hostname === "localhost";
  } catch {
    return false;
  }
}

async function randomToken(bytes: number): Promise<string> {
  const buf = new Uint8Array(bytes);
  crypto.getRandomValues(buf);
  return Array.from(buf, (b) => b.toString(16).padStart(2, "0")).join("");
}

async function pkceChallenge(verifier: string): Promise<string> {
  const data = new TextEncoder().encode(verifier);
  const hash = await crypto.subtle.digest("SHA-256", data);
  return base64urlencode(new Uint8Array(hash));
}

function base64urlencode(bytes: Uint8Array): string {
  let s = btoa(String.fromCharCode(...bytes));
  return s.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function pickClientId(provider: Provider, env: Bindings): string {
  switch (provider.id) {
    case "github": return env.GITHUB_CLIENT_ID;
    case "linear": return env.LINEAR_CLIENT_ID;
    case "notion": return env.NOTION_CLIENT_ID;
    default: throw new Error(`no client_id binding for ${provider.id}`);
  }
}

function pickClientSecret(provider: Provider, env: Bindings): string {
  if (provider.id === "notion") {
    if (!env.NOTION_CLIENT_SECRET) {
      throw new Error("NOTION_CLIENT_SECRET not configured");
    }
    return env.NOTION_CLIENT_SECRET;
  }
  throw new Error(`provider ${provider.id} does not use a client secret`);
}

function buildAuthorizeUrl(opts: {
  provider: Provider;
  clientId: string;
  challenge: string;
  state: string;
  redirectUri: string;
  scopes: string;
}): string {
  const url = new URL(opts.provider.authorizeUrl);
  url.searchParams.set("client_id", opts.clientId);
  url.searchParams.set("redirect_uri", opts.redirectUri);
  url.searchParams.set("response_type", "code");
  url.searchParams.set("scope", opts.scopes);
  url.searchParams.set("state", opts.state);
  if (opts.provider.usePkce) {
    url.searchParams.set("code_challenge", opts.challenge);
    url.searchParams.set("code_challenge_method", "S256");
  }
  if (opts.provider.id === "notion") {
    url.searchParams.set("owner", "user");
  }
  return url.toString();
}

async function exchangeToken(opts: {
  provider: Provider;
  code: string;
  verifier: string;
  clientId: string;
  clientSecret?: string;
  redirectUri: string;
}): Promise<Record<string, unknown>> {
  const body = new URLSearchParams({
    grant_type: "authorization_code",
    code: opts.code,
    redirect_uri: opts.redirectUri,
    client_id: opts.clientId,
  });
  if (opts.provider.usePkce) {
    body.set("code_verifier", opts.verifier);
  }
  if (opts.clientSecret) {
    body.set("client_secret", opts.clientSecret);
  }

  const resp = await fetch(opts.provider.tokenUrl, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
      "Accept": "application/json",
      "User-Agent": "plynf-oauth-broker/0.1",
    },
    body,
  });

  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`provider returned ${resp.status}: ${text}`);
  }

  // GitHub returns x-www-form-urlencoded by default unless Accept set
  const contentType = resp.headers.get("content-type") || "";
  if (contentType.includes("application/x-www-form-urlencoded")) {
    const text = await resp.text();
    const parsed: Record<string, string> = {};
    new URLSearchParams(text).forEach((v, k) => { parsed[k] = v; });
    return parsed;
  }
  return await resp.json();
}

export default app;
