// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

package plinth_test

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/plinth/sdk-go/plinth"
)

// TestIdentityIssueToken covers the happy path: a valid request flows
// through, the JWT + decoded claims come back, and the JTI is exposed.
func TestIdentityIssueToken(t *testing.T) {
	ms := NewMockServer(t)
	expires := time.Now().Add(1 * time.Hour).Format(time.RFC3339Nano)
	ms.JSON("POST", "/v1/tokens", 201, map[string]any{
		"token":      "eyJ.fake.jwt",
		"jti":        "tok_1",
		"expires_at": expires,
		"claims": map[string]any{
			"sub":       "agent-A",
			"iss":       "http://localhost:7425",
			"aud":       "plinth",
			"iat":       1,
			"exp":       2,
			"jti":       "tok_1",
			"agent_id":  "agent-A",
			"tenant_id": "default",
			"scopes":    []any{"tool:web.fetch:read"},
		},
	})

	c := newTestClient(t, ms)
	id, err := c.IdentityClient()
	if err != nil {
		t.Fatalf("IdentityClient: %v", err)
	}
	resp, err := id.IssueToken(context.Background(), plinth.TokenIssueRequest{
		AgentID:    "agent-A",
		Scopes:     []string{"tool:web.fetch:read"},
		TTLSeconds: 3600,
	})
	if err != nil {
		t.Fatalf("IssueToken: %v", err)
	}
	if resp.JTI != "tok_1" {
		t.Errorf("JTI = %q, want tok_1", resp.JTI)
	}
	if resp.Token != "eyJ.fake.jwt" {
		t.Errorf("Token = %q, want eyJ.fake.jwt", resp.Token)
	}
	if len(resp.Claims.Scopes) != 1 || resp.Claims.Scopes[0] != "tool:web.fetch:read" {
		t.Errorf("Claims.Scopes = %v, want [tool:web.fetch:read]", resp.Claims.Scopes)
	}
}

// TestIdentityVerifyToken covers the verify path.
func TestIdentityVerifyToken(t *testing.T) {
	ms := NewMockServer(t)
	ms.JSON("POST", "/v1/tokens/verify", 200, map[string]any{
		"sub":       "agent-A",
		"iss":       "http://localhost:7425",
		"aud":       "plinth",
		"iat":       1,
		"exp":       2,
		"jti":       "tok_1",
		"agent_id":  "agent-A",
		"tenant_id": "default",
		"scopes":    []any{"workspace:ws_x:read"},
	})

	c := newTestClient(t, ms)
	id, _ := c.IdentityClient()
	claims, err := id.VerifyToken(context.Background(), "eyJ.fake.jwt")
	if err != nil {
		t.Fatalf("VerifyToken: %v", err)
	}
	if claims.JTI != "tok_1" {
		t.Errorf("JTI = %q, want tok_1", claims.JTI)
	}
}

// TestIdentityVerifyExpiredSurfacesTyped covers the typed-error path:
// a TOKEN_EXPIRED envelope should match ErrTokenExpired.
func TestIdentityVerifyExpiredSurfacesTyped(t *testing.T) {
	ms := NewMockServer(t)
	ms.Error("POST", "/v1/tokens/verify", 401, "TOKEN_EXPIRED", "exp")
	c := newTestClient(t, ms)
	id, _ := c.IdentityClient()
	_, err := id.VerifyToken(context.Background(), "expired")
	if !errors.Is(err, plinth.ErrTokenExpired) {
		t.Errorf("err = %v, want ErrTokenExpired", err)
	}
	// Also satisfies the broader Unauthorized matcher? In our model
	// ErrTokenExpired and ErrUnauthorized are independent sentinels —
	// callers wanting to catch "any auth failure" should branch on
	// status code 401 or use multiple errors.Is checks. Not asserted
	// here.
}

// TestIdentityRevokeToken covers the DELETE-style revoke endpoint.
func TestIdentityRevokeToken(t *testing.T) {
	ms := NewMockServer(t)
	ms.On("DELETE", "/v1/tokens/tok_1/revoke", func(_ recordedRequest) mockResponse {
		return mockResponse{Status: 204}
	})
	c := newTestClient(t, ms)
	id, _ := c.IdentityClient()
	if err := id.RevokeToken(context.Background(), "tok_1"); err != nil {
		t.Fatalf("RevokeToken: %v", err)
	}
}

// TestIdentityListKeys covers the v0.4 signing-keys list.
func TestIdentityListKeys(t *testing.T) {
	ms := NewMockServer(t)
	now := time.Now().Format(time.RFC3339Nano)
	exp := time.Now().Add(30 * 24 * time.Hour).Format(time.RFC3339Nano)
	ms.JSON("GET", "/v1/keys", 200, map[string]any{
		"keys": []any{
			map[string]any{
				"kid":            "k1",
				"alg":            "RS256",
				"public_key_pem": "-----BEGIN…",
				"created_at":     now,
				"expires_at":     exp,
				"active":         true,
			},
		},
	})
	c := newTestClient(t, ms)
	id, _ := c.IdentityClient()
	keys, err := id.ListKeys(context.Background(), false)
	if err != nil {
		t.Fatalf("ListKeys: %v", err)
	}
	if len(keys) != 1 || keys[0].KID != "k1" || !keys[0].Active {
		t.Errorf("keys = %+v, want one active k1", keys)
	}
}

// TestIdentityNotConfiguredOnPlinth verifies that IdentityClient()
// fails fast when Config.IdentityURL was empty.
func TestIdentityNotConfiguredOnPlinth(t *testing.T) {
	c, err := plinth.New(plinth.Config{
		WorkspaceURL: "http://localhost:7421",
		GatewayURL:   "http://localhost:7422",
		APIKey:       "x",
	})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	if _, err := c.IdentityClient(); !errors.Is(err, plinth.ErrIdentityNotConfigured) {
		t.Errorf("err = %v, want ErrIdentityNotConfigured", err)
	}
}
