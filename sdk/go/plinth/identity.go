// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

package plinth

import (
	"context"
	"net/url"
)

// IdentityClient is the v0.3 identity-service surface (token issuance,
// verification, revocation; signing-key rotation; per-tenant quotas).
type IdentityClient struct {
	http *HTTPClient
}

// NewIdentityClient constructs a client bound to identityHTTP. Exported
// so tests can wire one up directly.
func NewIdentityClient(identityHTTP *HTTPClient) *IdentityClient {
	return &IdentityClient{http: identityHTTP}
}

// IssueToken mints a new capability token for the given agent.
func (c *IdentityClient) IssueToken(ctx context.Context, req TokenIssueRequest) (*TokenIssueResponse, error) {
	var resp TokenIssueResponse
	if err := c.http.PostJSON(ctx, "/v1/tokens", &resp, WithJSON(req)); err != nil {
		return nil, err
	}
	return &resp, nil
}

// VerifyToken validates a token and returns its decoded claims.
// Returns ErrInvalidToken / ErrTokenExpired / ErrTokenRevoked (each
// satisfies errors.Is on the matching sentinel) on failure.
func (c *IdentityClient) VerifyToken(ctx context.Context, token string) (*TokenClaims, error) {
	var claims TokenClaims
	err := c.http.PostJSON(
		ctx,
		"/v1/tokens/verify",
		&claims,
		WithJSON(map[string]any{"token": token}),
	)
	if err != nil {
		return nil, err
	}
	return &claims, nil
}

// RevokeToken revokes a token by its JTI.
func (c *IdentityClient) RevokeToken(ctx context.Context, jti string) error {
	return c.http.Delete(
		ctx,
		"/v1/tokens/"+EncodePathSegment(jti)+"/revoke",
	)
}

// GetTokenInfo fetches token metadata (no secret) by JTI.
func (c *IdentityClient) GetTokenInfo(ctx context.Context, jti string) (*TokenInfo, error) {
	var info TokenInfo
	err := c.http.GetJSON(
		ctx,
		"/v1/tokens/"+EncodePathSegment(jti),
		&info,
	)
	if err != nil {
		return nil, err
	}
	return &info, nil
}

// JWKS returns the public keys identity uses to sign tokens. Useful
// for clients verifying locally without round-tripping to identity on
// every request.
func (c *IdentityClient) JWKS(ctx context.Context) (map[string]any, error) {
	var jwks map[string]any
	if err := c.http.GetJSON(ctx, "/v1/.well-known/jwks.json", &jwks); err != nil {
		return nil, err
	}
	return jwks, nil
}

// ListKeys returns signing keys (public material only). For an HS256
// deployment this is empty — the secret isn't published.
func (c *IdentityClient) ListKeys(ctx context.Context, includeExpired bool) ([]SigningKey, error) {
	var resp struct {
		Keys []SigningKey `json:"keys"`
	}
	q := url.Values{}
	if includeExpired {
		q.Set("include_expired", "true")
	}
	err := c.http.GetJSON(ctx, "/v1/keys", &resp, WithQuery(q))
	if err != nil {
		return nil, err
	}
	return resp.Keys, nil
}

// RotateKey forces a key rotation. Returns the new active key.
func (c *IdentityClient) RotateKey(ctx context.Context) (*SigningKey, error) {
	var key SigningKey
	if err := c.http.PostJSON(ctx, "/v1/keys/rotate", &key); err != nil {
		return nil, err
	}
	return &key, nil
}

// ExpireKey force-expires a signing key (incident response).
func (c *IdentityClient) ExpireKey(ctx context.Context, kid string) error {
	return c.http.Delete(ctx, "/v1/keys/"+EncodePathSegment(kid))
}

// GetQuotas fetches the per-tenant quota envelope.
func (c *IdentityClient) GetQuotas(ctx context.Context, tenantID string) (*TenantQuotas, error) {
	var q TenantQuotas
	err := c.http.GetJSON(
		ctx,
		"/v1/tenants/"+EncodePathSegment(tenantID)+"/quotas",
		&q,
	)
	if err != nil {
		return nil, err
	}
	return &q, nil
}

// GetUsage returns the per-tenant usage rollup.
func (c *IdentityClient) GetUsage(ctx context.Context, tenantID string) (*TenantUsage, error) {
	var u TenantUsage
	err := c.http.GetJSON(
		ctx,
		"/v1/tenants/"+EncodePathSegment(tenantID)+"/usage",
		&u,
	)
	if err != nil {
		return nil, err
	}
	return &u, nil
}
