// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

package plinth

import (
	"context"
	"net/url"
	"strconv"
)

// KVProxy is the versioned key-value store for a workspace.
//
// Every Set creates a new immutable version. Reads default to the
// latest version; pass a version-aware variant to read a specific
// historical revision.
//
// Construct via *WorkspaceClient.KV.
type KVProxy struct {
	ws *WorkspaceClient
}

func newKVProxy(ws *WorkspaceClient) *KVProxy { return &KVProxy{ws: ws} }

// Set writes value to key and returns the resulting versioned entry.
// value is JSON-encoded — pass any type that encoding/json can marshal.
func (k *KVProxy) Set(ctx context.Context, key string, value any) (*KVEntry, error) {
	var entry KVEntry
	err := k.ws.http.PutJSON(
		ctx,
		k.path(key),
		&entry,
		WithJSON(map[string]any{"value": value}),
		WithQuery(k.ws.branchQuery()),
		WithNotFoundCode(ErrWorkspaceNotFound.Code),
	)
	if err != nil {
		return nil, err
	}
	return &entry, nil
}

// Get returns the latest decoded value for key. Returns
// (zero, ErrKeyNotFound) when the key was deleted or never set.
//
// The decoded type matches encoding/json's defaults (float64 for
// numbers, map[string]any for objects, etc.). For typed access use
// GetWithMeta and unmarshal entry.Value into your own struct.
func (k *KVProxy) Get(ctx context.Context, key string) (any, error) {
	entry, err := k.GetWithMeta(ctx, key)
	if err != nil {
		return nil, err
	}
	return entry.Value, nil
}

// GetWithVersion returns the latest value plus its monotonic version.
// Idiomatic Go shape: callers can ignore the version with `_, _ = ...`
// or branch on it for optimistic concurrency.
func (k *KVProxy) GetWithVersion(ctx context.Context, key string) (any, int, error) {
	entry, err := k.GetWithMeta(ctx, key)
	if err != nil {
		return nil, 0, err
	}
	return entry.Value, entry.Version, nil
}

// GetVersion returns a specific historical version of key. Returns
// ErrKeyNotFound when the version doesn't exist.
func (k *KVProxy) GetVersion(ctx context.Context, key string, version int) (*KVEntry, error) {
	q := k.ws.branchQuery()
	q.Set("version", strconv.Itoa(version))
	return k.fetchEntry(ctx, key, q)
}

// GetWithMeta returns the full KVEntry for key (latest version).
func (k *KVProxy) GetWithMeta(ctx context.Context, key string) (*KVEntry, error) {
	return k.fetchEntry(ctx, key, k.ws.branchQuery())
}

// History returns every recorded version of key (oldest first).
func (k *KVProxy) History(ctx context.Context, key string) ([]KVEntry, error) {
	var resp struct {
		Versions []KVEntry `json:"versions"`
	}
	err := k.ws.http.GetJSON(
		ctx,
		k.path(key)+"/history",
		&resp,
		WithQuery(k.ws.branchQuery()),
		WithNotFoundCode(ErrKeyNotFound.Code),
	)
	if err != nil {
		return nil, err
	}
	return resp.Versions, nil
}

// Delete tombstones key. Reads after this return ErrKeyNotFound.
func (k *KVProxy) Delete(ctx context.Context, key string) error {
	return k.ws.http.Delete(
		ctx,
		k.path(key),
		WithQuery(k.ws.branchQuery()),
		WithNotFoundCode(ErrKeyNotFound.Code),
	)
}

// List returns the latest entry for every key in the workspace.
func (k *KVProxy) List(ctx context.Context) ([]KVEntry, error) {
	var resp struct {
		Entries []KVEntry `json:"entries"`
	}
	err := k.ws.http.GetJSON(
		ctx,
		"/v1/workspaces/"+EncodePathSegment(k.ws.ID())+"/kv",
		&resp,
		WithQuery(k.ws.branchQuery()),
		WithNotFoundCode(ErrWorkspaceNotFound.Code),
	)
	if err != nil {
		return nil, err
	}
	return resp.Entries, nil
}

// fetchEntry is the shared GET-and-decode helper used by Get* methods.
func (k *KVProxy) fetchEntry(ctx context.Context, key string, q url.Values) (*KVEntry, error) {
	var entry KVEntry
	err := k.ws.http.GetJSON(
		ctx,
		k.path(key),
		&entry,
		WithQuery(q),
		WithNotFoundCode(ErrKeyNotFound.Code),
	)
	if err != nil {
		return nil, err
	}
	return &entry, nil
}

func (k *KVProxy) path(key string) string {
	return "/v1/workspaces/" + EncodePathSegment(k.ws.ID()) + "/kv/" + EncodePathSegment(key)
}
