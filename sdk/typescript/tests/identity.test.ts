/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 */

import { describe, expect, it } from "vitest";

import {
  InvalidArgumentsError,
  InvalidTokenError,
  Plinth,
  SigningKeyNotFoundError,
  TokenExpiredError,
  TokenRevokedError,
  UnauthorizedError,
  type SigningKey,
  type TokenClaims,
  type TokenIssueResponse,
} from "../src/index.js";
import { MockServer } from "./_helpers.js";

function makeClient(server: MockServer, identityUrl = "http://identity.test"): Plinth {
  return new Plinth({
    workspaceUrl: "http://workspace.test",
    gatewayUrl: "http://gateway.test",
    identityUrl,
    apiKey: "bootstrap-token",
    fetch: server.fetch as unknown as typeof fetch,
  });
}

const sampleClaims: TokenClaims = {
  sub: "agent_1",
  iss: "http://identity.test",
  aud: "plinth",
  iat: 1_700_000_000,
  exp: 1_700_003_600,
  jti: "jti_abc",
  agent_id: "agent_1",
  tenant_id: "default",
  workspace_id: null,
  scopes: ["tool:web.fetch:read"],
  rate_limit: null,
};

describe("IdentityClient — issue / verify / revoke", () => {
  it("issueToken POSTs the request body and returns the response", async () => {
    const server = new MockServer();
    server.on("POST", /\/v1\/tokens$/, (req) => {
      const body = JSON.parse(req.body ?? "{}");
      expect(body.agent_id).toBe("agent_1");
      expect(body.scopes).toEqual(["tool:web.fetch:read", "workspace:my-task:write"]);
      expect(body.tenant_id).toBe("default");
      expect(body.ttl_seconds).toBe(3600);
      return {
        status: 201,
        body: {
          token: "header.payload.sig",
          jti: "jti_abc",
          expires_at: "2026-01-01T01:00:00Z",
          claims: sampleClaims,
        } satisfies TokenIssueResponse,
      };
    });

    const client = makeClient(server);
    const issued = await client.identity.issueToken({
      agentId: "agent_1",
      scopes: ["tool:web.fetch:read", "workspace:my-task:write"],
      tenantId: "default",
      ttlSeconds: 3600,
    });
    expect(issued.token).toBe("header.payload.sig");
    expect(issued.claims.agent_id).toBe("agent_1");
  });

  it("issueToken omits optional fields not provided", async () => {
    const server = new MockServer();
    let captured: Record<string, unknown> = {};
    server.on("POST", /\/v1\/tokens$/, (req) => {
      captured = JSON.parse(req.body ?? "{}");
      return {
        status: 201,
        body: {
          token: "t",
          jti: "j",
          expires_at: "2026-01-01T00:00:00Z",
          claims: sampleClaims,
        },
      };
    });
    const client = makeClient(server);
    await client.identity.issueToken({ agentId: "a", scopes: ["s"] });
    expect(captured).toEqual({ agent_id: "a", scopes: ["s"] });
  });

  it("verifyToken POSTs the token and returns the claims", async () => {
    const server = new MockServer();
    server.on("POST", /\/v1\/tokens\/verify$/, (req) => {
      const body = JSON.parse(req.body ?? "{}");
      expect(body.token).toBe("xyz");
      return { body: sampleClaims };
    });
    const client = makeClient(server);
    const claims = await client.identity.verifyToken("xyz");
    expect(claims.jti).toBe("jti_abc");
    expect(claims.scopes).toEqual(["tool:web.fetch:read"]);
  });

  it("revokeToken POSTs to the revoke endpoint", async () => {
    const server = new MockServer();
    server.on("POST", /\/v1\/tokens\/jti_abc\/revoke$/, () => ({ status: 204 }));
    const client = makeClient(server);
    await expect(client.identity.revokeToken("jti_abc")).resolves.toBeUndefined();
  });

  it("getTokenInfo returns the token metadata view", async () => {
    const server = new MockServer();
    server.json("GET", /\/v1\/tokens\/jti_abc$/, {
      jti: "jti_abc",
      agent_id: "agent_1",
      tenant_id: "default",
      issued_at: "2026-01-01T00:00:00Z",
      expires_at: "2026-01-01T01:00:00Z",
      revoked: false,
      revoked_at: null,
      metadata: {},
    });
    const client = makeClient(server);
    const info = await client.identity.getTokenInfo("jti_abc");
    expect(info.revoked).toBe(false);
    expect(info.agent_id).toBe("agent_1");
  });

  it("getJwks returns the well-known JSON document", async () => {
    const server = new MockServer();
    server.json("GET", /\/\.well-known\/jwks\.json$/, { keys: [{ kid: "k1", alg: "RS256" }] });
    const client = makeClient(server);
    const jwks = await client.identity.getJwks();
    expect(Array.isArray((jwks as { keys: unknown[] }).keys)).toBe(true);
  });
});

describe("IdentityClient — error mapping", () => {
  it("maps INVALID_TOKEN to InvalidTokenError (subclass of UnauthorizedError)", async () => {
    const server = new MockServer();
    server.on("POST", /\/v1\/tokens\/verify$/, () => ({
      status: 401,
      body: { error: { code: "INVALID_TOKEN", message: "bad token" } },
    }));
    const client = makeClient(server);
    const err = await client.identity
      .verifyToken("bogus")
      .catch((e: unknown) => e as Error);
    expect(err).toBeInstanceOf(InvalidTokenError);
    expect(err).toBeInstanceOf(UnauthorizedError);
    expect((err as InvalidTokenError).code).toBe("INVALID_TOKEN");
  });

  it("maps TOKEN_EXPIRED to TokenExpiredError", async () => {
    const server = new MockServer();
    server.on("POST", /\/v1\/tokens\/verify$/, () => ({
      status: 401,
      body: { error: { code: "TOKEN_EXPIRED", message: "expired" } },
    }));
    const client = makeClient(server);
    await expect(client.identity.verifyToken("x")).rejects.toBeInstanceOf(TokenExpiredError);
  });

  it("maps TOKEN_REVOKED to TokenRevokedError", async () => {
    const server = new MockServer();
    server.on("POST", /\/v1\/tokens\/verify$/, () => ({
      status: 401,
      body: { error: { code: "TOKEN_REVOKED", message: "revoked" } },
    }));
    const client = makeClient(server);
    await expect(client.identity.verifyToken("x")).rejects.toBeInstanceOf(TokenRevokedError);
  });
});

describe("Plinth.identity — accessor", () => {
  it("throws when identityUrl was not configured", () => {
    const server = new MockServer();
    const client = new Plinth({
      workspaceUrl: "http://workspace.test",
      gatewayUrl: "http://gateway.test",
      apiKey: "bootstrap",
      fetch: server.fetch as unknown as typeof fetch,
    });
    expect(() => client.identity).toThrow(/identityUrl/);
  });
});

// ---------------------------------------------------------------------------
// v0.4 — Signing keys

const sampleKey: SigningKey = {
  kid: "abc1234567890def",
  alg: "RS256",
  public_key_pem: "-----BEGIN PUBLIC KEY-----\nfake\n-----END PUBLIC KEY-----\n",
  created_at: "2026-01-01T00:00:00Z",
  rotated_in_at: "2026-01-01T00:00:00Z",
  expires_at: "2026-03-01T00:00:00Z",
  active: true,
};

describe("IdentityClient — signing keys (v0.4)", () => {
  it("listKeys returns the keys array", async () => {
    const server = new MockServer();
    server.json("GET", /\/v1\/keys$/, { keys: [sampleKey] });
    const client = makeClient(server);
    const keys = await client.identity.listKeys();
    expect(keys).toHaveLength(1);
    expect(keys[0].kid).toBe("abc1234567890def");
    expect(keys[0].active).toBe(true);
  });

  it("listKeys passes include_expired when requested", async () => {
    const server = new MockServer();
    let capturedUrl = "";
    server.on("GET", /\/v1\/keys/, (req) => {
      capturedUrl = req.url;
      return { body: { keys: [] } };
    });
    const client = makeClient(server);
    await client.identity.listKeys({ includeExpired: true });
    expect(capturedUrl).toContain("include_expired=true");
  });

  it("listKeys returns empty list for HS256 deployments", async () => {
    const server = new MockServer();
    server.json("GET", /\/v1\/keys/, { keys: [] });
    const client = makeClient(server);
    const keys = await client.identity.listKeys();
    expect(keys).toEqual([]);
  });

  it("rotateKey returns the new active key", async () => {
    const server = new MockServer();
    server.on("POST", /\/v1\/keys\/rotate$/, () => ({
      status: 201,
      body: { ...sampleKey, kid: "newkid000000abcd" },
    }));
    const client = makeClient(server);
    const key = await client.identity.rotateKey();
    expect(key.kid).toBe("newkid000000abcd");
    expect(key.active).toBe(true);
  });

  it("rotateKey throws InvalidArgumentsError on HS256 deployments", async () => {
    const server = new MockServer();
    server.on("POST", /\/v1\/keys\/rotate$/, () => ({
      status: 400,
      body: {
        error: {
          code: "INVALID_ARGUMENTS",
          message: "key rotation is only available when jwt_alg=RS256",
          details: { jwt_alg: "HS256" },
        },
      },
    }));
    const client = makeClient(server);
    await expect(client.identity.rotateKey()).rejects.toBeInstanceOf(InvalidArgumentsError);
  });

  it("expireKey DELETEs the key", async () => {
    const server = new MockServer();
    let captured = "";
    server.on("DELETE", /\/v1\/keys\/abc1234567890def$/, (req) => {
      captured = req.method;
      return { status: 204 };
    });
    const client = makeClient(server);
    await expect(client.identity.expireKey("abc1234567890def")).resolves.toBeUndefined();
    expect(captured).toBe("DELETE");
  });

  it("expireKey unknown kid → SigningKeyNotFoundError", async () => {
    const server = new MockServer();
    server.on("DELETE", /\/v1\/keys\/.+$/, () => ({
      status: 404,
      body: {
        error: {
          code: "SIGNING_KEY_NOT_FOUND",
          message: "Signing key 'nope' does not exist",
          details: { kid: "nope" },
        },
      },
    }));
    const client = makeClient(server);
    await expect(client.identity.expireKey("nope")).rejects.toBeInstanceOf(
      SigningKeyNotFoundError,
    );
  });

  it("getKey returns matching key from the list", async () => {
    const server = new MockServer();
    server.json("GET", /\/v1\/keys/, {
      keys: [
        sampleKey,
        { ...sampleKey, kid: "second", active: false },
      ],
    });
    const client = makeClient(server);
    const key = await client.identity.getKey("second");
    expect(key.kid).toBe("second");
    expect(key.active).toBe(false);
  });

  it("getKey throws SigningKeyNotFoundError when the kid is missing", async () => {
    const server = new MockServer();
    server.json("GET", /\/v1\/keys/, { keys: [] });
    const client = makeClient(server);
    await expect(client.identity.getKey("missing")).rejects.toBeInstanceOf(
      SigningKeyNotFoundError,
    );
  });
});
