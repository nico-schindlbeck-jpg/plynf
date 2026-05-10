// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

// Package plinth_test exercises the Go SDK as an external consumer.
//
// External-test placement means we only see the public API — anything
// the SDK doesn't export is invisible here. Mirrors the layout the
// Python and TypeScript SDKs already adopt.
package plinth_test

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"

	"github.com/plinth/sdk-go/plinth"
)

// recordedRequest is a single inbound HTTP request captured by
// MockServer for later assertion.
type recordedRequest struct {
	Method  string
	Path    string
	RawPath string
	Query   string
	Headers http.Header
	Body    []byte
}

// jsonBody decodes the recorded body into out. Convenience for tests
// that want to assert on the JSON structure rather than the bytes.
func (r recordedRequest) jsonBody(t *testing.T, out any) {
	t.Helper()
	if len(r.Body) == 0 {
		t.Fatalf("recorded request has empty body, expected JSON")
	}
	if err := json.Unmarshal(r.Body, out); err != nil {
		t.Fatalf("decode recorded body: %v\nbody: %s", err, string(r.Body))
	}
}

// mockResponse is the shape of a scripted response.
type mockResponse struct {
	Status      int
	JSONBody    any
	RawBody     []byte
	ContentType string
	Headers     http.Header
}

// handler is a function that produces a response for an inbound
// request. Tests register handlers via MockServer.On.
type handler func(req recordedRequest) mockResponse

// route binds a method+path matcher to a handler.
type route struct {
	method   string
	path     string
	handler  handler
}

// MockServer is a tiny route-based httptest server that records every
// request and returns scripted responses.
//
// Mirrors the TS SDK's MockServer in tests/_helpers.ts. Build one per
// test — it uses httptest.NewServer so it owns its own port.
type MockServer struct {
	mu        sync.Mutex
	server    *httptest.Server
	requests  []recordedRequest
	routes    []route
	notFoundN int
}

// NewMockServer returns a started MockServer. Call .Close() when the
// test ends — usually via t.Cleanup.
func NewMockServer(t *testing.T) *MockServer {
	t.Helper()
	ms := &MockServer{}
	ms.server = httptest.NewServer(http.HandlerFunc(ms.serve))
	t.Cleanup(ms.Close)
	return ms
}

// URL returns the base URL of the server.
func (m *MockServer) URL() string { return m.server.URL }

// Close shuts down the underlying httptest.Server.
func (m *MockServer) Close() {
	if m.server != nil {
		m.server.Close()
	}
}

// On registers a handler for method+path. If multiple handlers match,
// the first one wins (so call site order matters).
func (m *MockServer) On(method, path string, h handler) *MockServer {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.routes = append(m.routes, route{method: method, path: path, handler: h})
	return m
}

// JSON is a convenience helper: register a JSON response for method+path.
func (m *MockServer) JSON(method, path string, status int, body any) *MockServer {
	return m.On(method, path, func(_ recordedRequest) mockResponse {
		return mockResponse{Status: status, JSONBody: body}
	})
}

// Error registers a Plinth-shaped error envelope response.
func (m *MockServer) Error(method, path string, status int, code, message string) *MockServer {
	return m.On(method, path, func(_ recordedRequest) mockResponse {
		return mockResponse{
			Status: status,
			JSONBody: map[string]any{
				"error": map[string]any{
					"code":    code,
					"message": message,
				},
			},
		}
	})
}

// ErrorWithDetails is like Error but with structured details.
func (m *MockServer) ErrorWithDetails(method, path string, status int, code, message string, details map[string]any) *MockServer {
	return m.On(method, path, func(_ recordedRequest) mockResponse {
		return mockResponse{
			Status: status,
			JSONBody: map[string]any{
				"error": map[string]any{
					"code":    code,
					"message": message,
					"details": details,
				},
			},
		}
	})
}

// Bytes registers a raw byte response (e.g. file contents).
func (m *MockServer) Bytes(method, path string, status int, body []byte, contentType string) *MockServer {
	return m.On(method, path, func(_ recordedRequest) mockResponse {
		return mockResponse{Status: status, RawBody: body, ContentType: contentType}
	})
}

// Requests returns a snapshot of all recorded requests so far.
func (m *MockServer) Requests() []recordedRequest {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make([]recordedRequest, len(m.requests))
	copy(out, m.requests)
	return out
}

// LastRequest returns the most recent request, or panics if there is
// none.
func (m *MockServer) LastRequest(t *testing.T) recordedRequest {
	t.Helper()
	reqs := m.Requests()
	if len(reqs) == 0 {
		t.Fatalf("no requests recorded")
	}
	return reqs[len(reqs)-1]
}

// NotFoundCount returns the number of inbound requests that didn't
// match any registered route. Useful for asserting that a test didn't
// silently fall through to a 404.
func (m *MockServer) NotFoundCount() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.notFoundN
}

// serve is the http.HandlerFunc body. Records the request, then either
// dispatches to a registered route or returns a Plinth-shaped 500.
func (m *MockServer) serve(w http.ResponseWriter, r *http.Request) {
	bodyBytes, _ := io.ReadAll(r.Body)
	r.Body.Close()

	rec := recordedRequest{
		Method:  r.Method,
		Path:    r.URL.Path,
		RawPath: r.URL.RawPath,
		Query:   r.URL.RawQuery,
		Headers: r.Header.Clone(),
		Body:    bodyBytes,
	}

	m.mu.Lock()
	m.requests = append(m.requests, rec)
	var matched *route
	for i := range m.routes {
		if m.routes[i].method == r.Method && m.routes[i].path == r.URL.Path {
			matched = &m.routes[i]
			break
		}
	}
	if matched == nil {
		m.notFoundN++
	}
	m.mu.Unlock()

	if matched == nil {
		writeError(w, 500, "INTERNAL_ERROR",
			"no MockServer route registered for "+r.Method+" "+r.URL.Path)
		return
	}

	resp := matched.handler(rec)
	writeMock(w, resp)
}

// writeMock serialises a mockResponse onto the wire.
func writeMock(w http.ResponseWriter, resp mockResponse) {
	for k, vs := range resp.Headers {
		for _, v := range vs {
			w.Header().Add(k, v)
		}
	}

	if resp.RawBody != nil {
		ct := resp.ContentType
		if ct == "" {
			ct = "application/octet-stream"
		}
		w.Header().Set("Content-Type", ct)
		status := resp.Status
		if status == 0 {
			status = 200
		}
		w.WriteHeader(status)
		_, _ = w.Write(resp.RawBody)
		return
	}

	if resp.JSONBody == nil {
		status := resp.Status
		if status == 0 {
			status = 204
		}
		w.WriteHeader(status)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	status := resp.Status
	if status == 0 {
		status = 200
	}
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(resp.JSONBody)
}

// writeError is a convenience for the no-route fallthrough.
func writeError(w http.ResponseWriter, status int, code, message string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]any{
		"error": map[string]any{"code": code, "message": message},
	})
}

// newTestClient is the canonical client builder for tests. Wires the
// passed-in MockServer URL into every service.
func newTestClient(t *testing.T, ms *MockServer) *plinth.Plinth {
	t.Helper()
	c, err := plinth.New(plinth.Config{
		WorkspaceURL: ms.URL(),
		GatewayURL:   ms.URL(),
		IdentityURL:  ms.URL(),
		APIKey:       "test-key",
	})
	if err != nil {
		t.Fatalf("plinth.New: %v", err)
	}
	return c
}
