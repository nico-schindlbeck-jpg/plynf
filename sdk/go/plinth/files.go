// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

package plinth

import (
	"context"
	"net/url"
	"strconv"
)

// Default content types used by FilesProxy.Write when the caller
// doesn't supply one explicitly.
const (
	defaultTextContentType   = "text/plain; charset=utf-8"
	defaultBinaryContentType = "application/octet-stream"
)

// FileWriteOptions tweaks a single FilesProxy.Write call. Currently
// only ContentType — kept as a struct for forward-compatible
// extensions (size hints, encoding hints, etc.).
type FileWriteOptions struct {
	// ContentType overrides the auto-detected MIME type. Defaults to
	// "application/octet-stream" for raw bytes.
	ContentType string
}

// FilesProxy is the versioned blob store for a workspace.
type FilesProxy struct {
	ws *WorkspaceClient
}

func newFilesProxy(ws *WorkspaceClient) *FilesProxy { return &FilesProxy{ws: ws} }

// Write uploads content to path. opts may be nil; defaults to
// "application/octet-stream" for the Content-Type.
//
// To upload text, use WriteText (which sets the right MIME type and
// avoids forcing callers to allocate a []byte explicitly).
func (f *FilesProxy) Write(ctx context.Context, path string, content []byte, opts *FileWriteOptions) (*FileEntry, error) {
	contentType := defaultBinaryContentType
	if opts != nil && opts.ContentType != "" {
		contentType = opts.ContentType
	}
	return f.write(ctx, path, content, contentType)
}

// WriteText is sugar for uploading UTF-8 text. Sets Content-Type to
// "text/plain; charset=utf-8" by default; pass a non-nil opts to
// override.
func (f *FilesProxy) WriteText(ctx context.Context, path, content string, opts *FileWriteOptions) (*FileEntry, error) {
	contentType := defaultTextContentType
	if opts != nil && opts.ContentType != "" {
		contentType = opts.ContentType
	}
	return f.write(ctx, path, []byte(content), contentType)
}

// Read fetches the raw bytes of path (latest version). Returns
// ErrFileNotFound when the file doesn't exist.
func (f *FilesProxy) Read(ctx context.Context, path string) ([]byte, error) {
	return f.read(ctx, path, f.ws.branchQuery())
}

// ReadVersion fetches a specific historical version of path.
func (f *FilesProxy) ReadVersion(ctx context.Context, path string, version int) ([]byte, error) {
	q := f.ws.branchQuery()
	q.Set("version", strconv.Itoa(version))
	return f.read(ctx, path, q)
}

// ReadText fetches path and decodes it as UTF-8 text. Convenience for
// callers who don't want to deal with []byte.
func (f *FilesProxy) ReadText(ctx context.Context, path string) (string, error) {
	b, err := f.Read(ctx, path)
	if err != nil {
		return "", err
	}
	return string(b), nil
}

// Meta returns metadata about path without downloading the bytes.
func (f *FilesProxy) Meta(ctx context.Context, path string) (*FileEntry, error) {
	var entry FileEntry
	err := f.ws.http.GetJSON(
		ctx,
		f.path(path)+"/meta",
		&entry,
		WithQuery(f.ws.branchQuery()),
		WithNotFoundCode(ErrFileNotFound.Code),
	)
	if err != nil {
		return nil, err
	}
	return &entry, nil
}

// Delete tombstones the file at path.
func (f *FilesProxy) Delete(ctx context.Context, path string) error {
	return f.ws.http.Delete(
		ctx,
		f.path(path),
		WithQuery(f.ws.branchQuery()),
		WithNotFoundCode(ErrFileNotFound.Code),
	)
}

// List returns metadata for every file in the workspace.
func (f *FilesProxy) List(ctx context.Context) ([]FileEntry, error) {
	var resp struct {
		Files []FileEntry `json:"files"`
	}
	err := f.ws.http.GetJSON(
		ctx,
		"/v1/workspaces/"+EncodePathSegment(f.ws.ID())+"/files",
		&resp,
		WithQuery(f.ws.branchQuery()),
		WithNotFoundCode(ErrWorkspaceNotFound.Code),
	)
	if err != nil {
		return nil, err
	}
	return resp.Files, nil
}

// write is the shared PUT helper for binary + text uploads.
func (f *FilesProxy) write(ctx context.Context, path string, body []byte, contentType string) (*FileEntry, error) {
	var entry FileEntry
	err := f.ws.http.PutJSON(
		ctx,
		f.path(path),
		&entry,
		WithBody(body, contentType),
		WithQuery(f.ws.branchQuery()),
		WithNotFoundCode(ErrWorkspaceNotFound.Code),
	)
	if err != nil {
		return nil, err
	}
	return &entry, nil
}

func (f *FilesProxy) read(ctx context.Context, path string, q url.Values) ([]byte, error) {
	return f.ws.http.Get(
		ctx,
		f.path(path),
		WithQuery(q),
		WithNotFoundCode(ErrFileNotFound.Code),
	)
}

func (f *FilesProxy) path(p string) string {
	return "/v1/workspaces/" + EncodePathSegment(f.ws.ID()) + "/files/" + EncodeFilePath(p)
}
