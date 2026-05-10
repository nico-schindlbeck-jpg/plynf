// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

package plinth

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
)

// HTTPClient is the internal request helper bound to a single Plinth
// service base URL. The Plinth facade owns one per backing service
// (workspace, gateway, identity).
//
// Public API surface intentionally uses the standard library: any
// *http.Client passed in via Config.HTTPClient is used as-is, so
// callers can wire up custom transports, retries, or instrumentation.
type HTTPClient struct {
	baseURL    string
	apiKey     string
	userAgent  string
	httpClient *http.Client
}

// NewHTTPClient returns a Plinth HTTPClient bound to baseURL.
//
// The userAgent and apiKey are attached to every outgoing request.
// httpClient must be non-nil — callers should pass a standard
// *http.Client with a reasonable timeout (the Plinth facade defaults
// to 30s when none is supplied).
func NewHTTPClient(httpClient *http.Client, baseURL, apiKey, userAgent string) *HTTPClient {
	return &HTTPClient{
		baseURL:    strings.TrimRight(baseURL, "/"),
		apiKey:     apiKey,
		userAgent:  userAgent,
		httpClient: httpClient,
	}
}

// BaseURL returns the base URL the client was constructed with (no
// trailing slash). Useful for diagnostic logging.
func (c *HTTPClient) BaseURL() string { return c.baseURL }

// requestOptions bundles the optional flags accepted by the verb
// helpers. Kept private — callers use Get/Post/Put/Delete and the JSON
// variants instead.
type requestOptions struct {
	body        any              // marshaled to JSON when set
	rawBody     []byte           // sent as-is when set (overrides body)
	contentType string           // overrides default Content-Type
	query       url.Values       // attached as ?key=value
	headers     map[string]string // extra request headers
	notFoundCode string          // override "NOT_FOUND" sentinel for 404s
}

// RequestOption mutates a requestOptions before the request is built.
// This package-private functional-options pattern keeps the verb method
// signatures small while still allowing every flag the Python SDK
// exposes via kwargs.
type RequestOption func(*requestOptions)

// WithJSON attaches a JSON body to the request.
func WithJSON(body any) RequestOption {
	return func(o *requestOptions) { o.body = body }
}

// WithBody attaches a raw byte body. Use for file uploads where the
// caller has already produced bytes and a content type.
func WithBody(b []byte, contentType string) RequestOption {
	return func(o *requestOptions) {
		o.rawBody = b
		if contentType != "" {
			o.contentType = contentType
		}
	}
}

// WithQuery attaches query parameters. Nil values are dropped.
func WithQuery(q url.Values) RequestOption {
	return func(o *requestOptions) { o.query = q }
}

// WithHeader attaches a single extra HTTP header to the request.
// Multiple WithHeader options compose.
func WithHeader(key, value string) RequestOption {
	return func(o *requestOptions) {
		if o.headers == nil {
			o.headers = make(map[string]string)
		}
		o.headers[key] = value
	}
}

// WithNotFoundCode hints the resource-specific error code to attach
// when the server returns a 404 without a code in the envelope. This
// matches the Python SDK's not_found_class kwarg.
func WithNotFoundCode(code string) RequestOption {
	return func(o *requestOptions) { o.notFoundCode = code }
}

// Get issues a GET request and returns the raw response body bytes.
//
// Non-2xx responses are mapped to *PlinthError before this returns.
func (c *HTTPClient) Get(ctx context.Context, path string, opts ...RequestOption) ([]byte, error) {
	return c.do(ctx, http.MethodGet, path, opts...)
}

// GetJSON issues a GET request and decodes the JSON body into out.
// out should be a pointer; passing a non-pointer is a programmer error
// and will surface as a json.Unmarshal failure.
func (c *HTTPClient) GetJSON(ctx context.Context, path string, out any, opts ...RequestOption) error {
	body, err := c.Get(ctx, path, opts...)
	if err != nil {
		return err
	}
	if out == nil {
		return nil
	}
	return decodeJSON(body, out)
}

// Post issues a POST request and returns the raw response body bytes.
func (c *HTTPClient) Post(ctx context.Context, path string, opts ...RequestOption) ([]byte, error) {
	return c.do(ctx, http.MethodPost, path, opts...)
}

// PostJSON issues a POST request and decodes the JSON body into out.
func (c *HTTPClient) PostJSON(ctx context.Context, path string, out any, opts ...RequestOption) error {
	body, err := c.Post(ctx, path, opts...)
	if err != nil {
		return err
	}
	if out == nil {
		return nil
	}
	return decodeJSON(body, out)
}

// Put issues a PUT request and returns the raw response body bytes.
func (c *HTTPClient) Put(ctx context.Context, path string, opts ...RequestOption) ([]byte, error) {
	return c.do(ctx, http.MethodPut, path, opts...)
}

// PutJSON issues a PUT request and decodes the JSON body into out.
func (c *HTTPClient) PutJSON(ctx context.Context, path string, out any, opts ...RequestOption) error {
	body, err := c.Put(ctx, path, opts...)
	if err != nil {
		return err
	}
	if out == nil {
		return nil
	}
	return decodeJSON(body, out)
}

// Patch issues a PATCH request and returns the raw response body bytes.
func (c *HTTPClient) Patch(ctx context.Context, path string, opts ...RequestOption) ([]byte, error) {
	return c.do(ctx, http.MethodPatch, path, opts...)
}

// PatchJSON issues a PATCH request and decodes the JSON body into out.
func (c *HTTPClient) PatchJSON(ctx context.Context, path string, out any, opts ...RequestOption) error {
	body, err := c.Patch(ctx, path, opts...)
	if err != nil {
		return err
	}
	if out == nil {
		return nil
	}
	return decodeJSON(body, out)
}

// Delete issues a DELETE request and discards the response body.
func (c *HTTPClient) Delete(ctx context.Context, path string, opts ...RequestOption) error {
	_, err := c.do(ctx, http.MethodDelete, path, opts...)
	return err
}

// do is the single request entry point used by every verb helper. It
// builds the *http.Request, sends it, decodes the standard error
// envelope on non-2xx, and returns the raw body on success.
func (c *HTTPClient) do(ctx context.Context, method, path string, opts ...RequestOption) ([]byte, error) {
	o := &requestOptions{}
	for _, opt := range opts {
		opt(o)
	}

	endpoint, err := c.buildURL(path, o.query)
	if err != nil {
		return nil, &PlinthError{Code: ErrInternal.Code, Message: err.Error(), Cause: err}
	}

	var bodyReader io.Reader
	contentType := ""
	switch {
	case o.rawBody != nil:
		bodyReader = bytes.NewReader(o.rawBody)
		contentType = o.contentType
		if contentType == "" {
			contentType = "application/octet-stream"
		}
	case o.body != nil:
		buf, marshalErr := json.Marshal(o.body)
		if marshalErr != nil {
			return nil, &PlinthError{
				Code:    ErrInternal.Code,
				Message: fmt.Sprintf("marshal request body: %v", marshalErr),
				Cause:   marshalErr,
			}
		}
		bodyReader = bytes.NewReader(buf)
		contentType = "application/json"
	}

	req, err := http.NewRequestWithContext(ctx, method, endpoint, bodyReader)
	if err != nil {
		return nil, &PlinthError{Code: ErrInternal.Code, Message: err.Error(), Cause: err}
	}
	req.Header.Set("Authorization", "Bearer "+c.apiKey)
	req.Header.Set("Accept", "application/json, application/octet-stream")
	if c.userAgent != "" {
		req.Header.Set("User-Agent", c.userAgent)
	}
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	for k, v := range o.headers {
		req.Header.Set(k, v)
	}

	resp, err := c.httpClient.Do(req)
	if err != nil {
		// net errors include context cancellation, DNS failure, etc.
		// Wrap so callers can errors.Is(err, plinth.ErrConnectionError).
		return nil, &PlinthError{
			Code:    ErrConnectionError.Code,
			Message: fmt.Sprintf("connection error to %s: %v", endpoint, err),
			Cause:   err,
		}
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, &PlinthError{
			Code:    ErrConnectionError.Code,
			Message: fmt.Sprintf("read response body from %s: %v", endpoint, err),
			Cause:   err,
		}
	}

	if resp.StatusCode >= 200 && resp.StatusCode < 300 {
		return body, nil
	}

	return nil, c.errorForResponse(resp, body, o.notFoundCode)
}

// buildURL joins the request path onto baseURL and appends any non-nil
// query parameters. Nil/empty values in the url.Values are skipped to
// avoid emitting "?key=" dangling fragments — matches the Python SDK's
// _clean_params helper.
func (c *HTTPClient) buildURL(path string, query url.Values) (string, error) {
	if !strings.HasPrefix(path, "/") {
		path = "/" + path
	}
	full := c.baseURL + path
	if len(query) == 0 {
		return full, nil
	}
	cleaned := url.Values{}
	for k, vs := range query {
		for _, v := range vs {
			if v == "" {
				continue
			}
			cleaned.Add(k, v)
		}
	}
	if len(cleaned) == 0 {
		return full, nil
	}
	separator := "?"
	if strings.Contains(full, "?") {
		separator = "&"
	}
	return full + separator + cleaned.Encode(), nil
}

// errorForResponse decodes the standard {"error": {...}} envelope and
// returns a *PlinthError with the right Code/Sentinel attached.
func (c *HTTPClient) errorForResponse(resp *http.Response, body []byte, fallbackNotFoundCode string) error {
	envelope := struct {
		Error struct {
			Code    string         `json:"code"`
			Message string         `json:"message"`
			Details map[string]any `json:"details"`
		} `json:"error"`
	}{}
	_ = json.Unmarshal(body, &envelope) // best-effort

	code := envelope.Error.Code
	message := envelope.Error.Message
	details := envelope.Error.Details

	// If the server didn't include a code, fall back first to the
	// caller-supplied 404 hint, then to the status-code map.
	if code == "" {
		if resp.StatusCode == http.StatusNotFound && fallbackNotFoundCode != "" {
			code = fallbackNotFoundCode
		} else if mapped, ok := statusToCode[resp.StatusCode]; ok {
			code = mapped
		} else {
			code = ErrInternal.Code
		}
	}

	if message == "" {
		if len(body) > 0 {
			message = strings.TrimSpace(string(body))
		} else {
			message = http.StatusText(resp.StatusCode)
		}
	}

	pe := &PlinthError{
		Code:       code,
		Message:    message,
		Details:    details,
		StatusCode: resp.StatusCode,
		Body:       body,
	}

	// Rate-limit responses carry retry hints in the envelope and/or
	// the Retry-After header. Surface them on the PlinthError so
	// callers can sleep without re-reading the response.
	if resp.StatusCode == http.StatusTooManyRequests || code == "RATE_LIMITED" || code == "COST_CAP_EXCEEDED" {
		pe.RetryAfter = parseRetryAfter(resp, details)
		if lt, ok := details["limit_type"].(string); ok {
			pe.LimitType = lt
		}
	}

	// errors.Is dispatch happens automatically: PlinthError.Is matches
	// on Code, so the returned *PlinthError satisfies the matching
	// sentinel without us having to wrap a Cause. We do consult the
	// table to canonicalise legacy aliases (e.g. server-side
	// "LOCK_HELD" → SDK-side "LOCK_CONFLICT") so callers can rely on
	// a single code per concept.
	if sentinel, ok := codeToSentinel[code]; ok && sentinel.Code != code {
		pe.Code = sentinel.Code
	}
	return pe
}

// parseRetryAfter extracts a retry hint (seconds) from the response.
// Prefers details.retry_after_seconds, falls back to the Retry-After
// header. Returns 0 when neither is present or parsable.
func parseRetryAfter(resp *http.Response, details map[string]any) float64 {
	if details != nil {
		switch raw := details["retry_after_seconds"].(type) {
		case float64:
			return raw
		case int:
			return float64(raw)
		case int64:
			return float64(raw)
		case string:
			if v, err := strconv.ParseFloat(raw, 64); err == nil {
				return v
			}
		}
	}
	header := resp.Header.Get("Retry-After")
	if header == "" {
		return 0
	}
	if v, err := strconv.ParseFloat(header, 64); err == nil {
		return v
	}
	return 0
}

// decodeJSON wraps json.Unmarshal with a PlinthError on failure so
// every error returned by the SDK is uniformly typed.
func decodeJSON(body []byte, out any) error {
	if len(body) == 0 {
		return nil
	}
	if err := json.Unmarshal(body, out); err != nil {
		return &PlinthError{
			Code:    ErrInternal.Code,
			Message: fmt.Sprintf("decode JSON: %v", err),
			Cause:   err,
			Body:    body,
		}
	}
	return nil
}

// EncodePathSegment percent-encodes a single path segment. Mirrors
// the Python SDK's _ek helper for KV keys and similar opaque values.
//
// Use this — not url.PathEscape — when the caller-provided string may
// contain "/" but should be treated as a single segment.
func EncodePathSegment(s string) string {
	return url.PathEscape(s)
}

// EncodeFilePath percent-encodes a file path, preserving "/" as the
// segment separator. Mirrors the Python SDK's _ep helper.
func EncodeFilePath(p string) string {
	parts := strings.Split(strings.TrimLeft(p, "/"), "/")
	for i, part := range parts {
		parts[i] = url.PathEscape(part)
	}
	return strings.Join(parts, "/")
}

// EncodeLockName percent-encodes a lock name while preserving both "/"
// and ":" so the workspace's {name:path} route can consume the whole
// thing as one segment.
func EncodeLockName(name string) string {
	parts := strings.Split(strings.TrimLeft(name, "/"), "/")
	for i, part := range parts {
		// Re-allow ":" since it's a valid sub-delimiter inside a path
		// component per RFC 3986.
		parts[i] = strings.ReplaceAll(url.PathEscape(part), "%3A", ":")
	}
	return strings.Join(parts, "/")
}

// IsCode reports whether err carries the given Plinth error code.
// Convenience helper for code-driven branching without writing a full
// errors.As block.
func IsCode(err error, code string) bool {
	var pe *PlinthError
	if !errors.As(err, &pe) {
		return false
	}
	return pe.Code == code
}
