/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 *
 * Identity client — issue, verify, and revoke capability tokens.
 *
 * Wraps the v0.3 identity service at `http://localhost:7425` (or wherever
 * `identityUrl` points). Tokens are JWTs (HS256 in v0.3) carrying scopes
 * like `tool:web.fetch:read` and `workspace:my-task:write` — the workspace
 * and gateway services delegate auth to identity.
 *
 * Mirrors the Python SDK's `client.identity.*` surface.
 */

import { SigningKeyNotFoundError } from "./errors.js";
import { encodePath, type HttpClient } from "./http.js";
import type {
  JsonValue,
  SigningKey,
  TokenClaims,
  TokenInfo,
  TokenIssueRequest,
  TokenIssueResponse,
} from "./types.js";

/**
 * Public surface for `client.identity`.
 *
 * Constructed lazily by the {@link Plinth} facade — only when the user
 * configured an `identityUrl`. Calls into identity follow the same auth
 * model as the rest of the SDK (`Authorization: Bearer …`), so a
 * bootstrap token is required to mint scoped child tokens.
 */
export class IdentityClient {
  constructor(private readonly http: HttpClient) {}

  /**
   * Mint a new capability token.
   *
   * @returns the encoded JWT plus its decoded claims and revocation
   *          handle (`jti`).
   */
  async issueToken(opts: IssueTokenOptions): Promise<TokenIssueResponse> {
    const body: TokenIssueRequest = {
      agent_id: opts.agentId,
      scopes: [...opts.scopes],
    };
    if (opts.tenantId !== undefined) body.tenant_id = opts.tenantId;
    if (opts.workspaceId !== undefined) body.workspace_id = opts.workspaceId;
    if (opts.ttlSeconds !== undefined) body.ttl_seconds = opts.ttlSeconds;
    if (opts.metadata !== undefined) body.metadata = opts.metadata;

    return this.http.requestJson<TokenIssueResponse>({
      method: "POST",
      path: "/v1/tokens",
      json: body as unknown as JsonValue,
    });
  }

  /**
   * Verify a token and return its claims.
   *
   * Throws {@link InvalidTokenError}, {@link TokenExpiredError}, or
   * {@link TokenRevokedError} (all subclasses of
   * {@link UnauthorizedError}) on failure.
   */
  async verifyToken(token: string): Promise<TokenClaims> {
    return this.http.requestJson<TokenClaims>({
      method: "POST",
      path: "/v1/tokens/verify",
      json: { token },
    });
  }

  /** Revoke a token by its `jti`. */
  async revokeToken(jti: string): Promise<void> {
    await this.http.requestVoid({
      method: "POST",
      path: `/v1/tokens/${encodePath(jti)}/revoke`,
    });
  }

  /** Fetch token metadata (no secret) by `jti`. */
  async getTokenInfo(jti: string): Promise<TokenInfo> {
    return this.http.requestJson<TokenInfo>({
      method: "GET",
      path: `/v1/tokens/${encodePath(jti)}`,
    });
  }

  /**
   * Return the JWKS (public keys) that identity uses to sign tokens.
   *
   * Useful for clients that want to verify locally without round-tripping
   * to identity on every request.
   */
  async getJwks(): Promise<Record<string, JsonValue>> {
    return this.http.requestJson<Record<string, JsonValue>>({
      method: "GET",
      path: "/v1/.well-known/jwks.json",
    });
  }

  // ---------------------------------------------------------------- v0.4 keys

  /**
   * List signing keys (public material only).
   *
   * For an HS256 deployment the identity service returns an empty list —
   * the secret isn't published.
   */
  async listKeys(opts?: { includeExpired?: boolean }): Promise<SigningKey[]> {
    const response = await this.http.requestJson<{ keys?: SigningKey[] }>({
      method: "GET",
      path: "/v1/keys",
      query: opts?.includeExpired ? { include_expired: "true" } : undefined,
    });
    return response.keys ?? [];
  }

  /**
   * Look up a single signing key by `kid`.
   *
   * Identity does not expose a per-key GET endpoint, so this list-and-filter
   * approach matches the Python SDK's behaviour. Throws
   * {@link SigningKeyNotFoundError} when the key isn't present.
   */
  async getKey(kid: string): Promise<SigningKey> {
    const keys = await this.listKeys({ includeExpired: true });
    const match = keys.find((k) => k.kid === kid);
    if (!match) {
      throw new SigningKeyNotFoundError(`Signing key ${JSON.stringify(kid)} does not exist`);
    }
    return match;
  }

  /**
   * Force a key rotation. Returns the new active key.
   *
   * Throws {@link InvalidArgumentsError} when identity is in HS256 mode.
   */
  async rotateKey(): Promise<SigningKey> {
    return this.http.requestJson<SigningKey>({
      method: "POST",
      path: "/v1/keys/rotate",
    });
  }

  /**
   * Force-expire a signing key (incident response).
   *
   * After expiry, tokens signed with this key fail verification on the
   * workspace + gateway as soon as their cached JWKS is refreshed.
   */
  async expireKey(kid: string): Promise<void> {
    await this.http.requestVoid({
      method: "DELETE",
      path: `/v1/keys/${encodePath(kid)}`,
    });
  }
}

/** Argument shape for {@link IdentityClient.issueToken}. */
export interface IssueTokenOptions {
  /** The agent the token represents. Becomes the `sub` claim. */
  agentId: string;
  /** Scope grammar — see CONTRACTS.md (e.g. `tool:web.fetch:read`). */
  scopes: string[];
  /** Tenant the token belongs to. Defaults to `"default"` server-side. */
  tenantId?: string;
  /** Optional workspace binding (`workspace:<id>:…` shortcut). */
  workspaceId?: string;
  /** Token lifetime in seconds. Defaults to 3600 server-side. */
  ttlSeconds?: number;
  /** Free-form metadata recorded with the token. */
  metadata?: Record<string, JsonValue>;
}
