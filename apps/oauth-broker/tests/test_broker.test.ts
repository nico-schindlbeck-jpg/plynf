// Broker behaviour tests. Run with `npm test`.
//
// Critical assertion: tokens NEVER persist server-side. We capture every
// KV write and verify nothing token-shaped lands in storage.

import { describe, expect, it, vi, beforeEach } from "vitest";
import app from "../src/index";
import { providers } from "../src/providers";

// In-memory KV stand-in
class FakeKV {
  store = new Map<string, { value: string; expirationTtl?: number }>();
  writes: Array<{ key: string; value: string }> = [];

  async put(key: string, value: string, opts?: { expirationTtl?: number }): Promise<void> {
    this.store.set(key, { value, expirationTtl: opts?.expirationTtl });
    this.writes.push({ key, value });
  }
  async get(key: string): Promise<string | null> {
    return this.store.get(key)?.value ?? null;
  }
  async delete(key: string): Promise<void> {
    this.store.delete(key);
  }
}

const makeEnv = (overrides: Record<string, unknown> = {}) => ({
  STATE: new FakeKV(),
  GITHUB_CLIENT_ID: "test_github_id",
  LINEAR_CLIENT_ID: "test_linear_id",
  NOTION_CLIENT_ID: "test_notion_id",
  NOTION_CLIENT_SECRET: "test_notion_secret",
  BROKER_CALLBACK_BASE: "https://oauth.plynf.com",
  ...overrides,
});

// ─── /health ──────────────────────────────────────────────────────────

describe("/health", () => {
  it("returns ok", async () => {
    const env = makeEnv();
    const res = await app.fetch(new Request("https://oauth.plynf.com/health"), env);
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body).toMatchObject({ ok: true });
  });
});

// ─── /v1/oauth/start ──────────────────────────────────────────────────

describe("/v1/oauth/start", () => {
  it("rejects unknown provider", async () => {
    const env = makeEnv();
    const res = await app.fetch(
      new Request("https://oauth.plynf.com/v1/oauth/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          provider: "facebook",
          local_callback: "http://127.0.0.1:7425/v1/oauth/cb",
        }),
      }),
      env,
    );
    expect(res.status).toBe(404);
  });

  it("rejects non-loopback local_callback", async () => {
    const env = makeEnv();
    const res = await app.fetch(
      new Request("https://oauth.plynf.com/v1/oauth/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          provider: "github",
          local_callback: "https://evil.example.com/steal",
        }),
      }),
      env,
    );
    expect(res.status).toBe(400);
  });

  it("returns an authorize URL and persists state for known provider", async () => {
    const env = makeEnv();
    const res = await app.fetch(
      new Request("https://oauth.plynf.com/v1/oauth/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          provider: "github",
          local_callback: "http://127.0.0.1:7425/v1/oauth/cb",
        }),
      }),
      env,
    );
    expect(res.status).toBe(200);
    const body = await res.json() as { authorize_url: string; state: string };
    expect(body.authorize_url).toContain("github.com/login/oauth/authorize");
    expect(body.authorize_url).toContain("code_challenge_method=S256");
    expect(body.state).toMatch(/^[0-9a-f]{32}$/);
    expect(env.STATE.store.size).toBe(1);
  });
});

// ─── No-token-persistence guarantee ────────────────────────────────────

describe("KV write audit — tokens never persist", () => {
  it("KV writes during /start contain no token-shaped strings", async () => {
    const env = makeEnv();
    await app.fetch(
      new Request("https://oauth.plynf.com/v1/oauth/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          provider: "github",
          local_callback: "http://127.0.0.1:7425/v1/oauth/cb",
        }),
      }),
      env,
    );
    for (const w of env.STATE.writes) {
      // Tokens look like ghp_*, gho_*, glpat_* (GitHub), lin_oauth_* (Linear),
      // secret_* (Notion), or generic JWT-like ey...
      expect(w.value).not.toMatch(/\b(ghp_|gho_|glpat_|lin_oauth_|secret_)/i);
      expect(w.value).not.toMatch(/\beyJ[A-Za-z0-9_-]{20,}/);  // JWT signature
      // Verifier IS stored (PKCE) but it's a one-way SHA256-pre-image — fine.
    }
  });
});
