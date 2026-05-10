// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

package plinth

import (
	"context"
	"strconv"
)

// ChannelSendOpts bundles the optional send-time metadata accepted by
// ChannelsProxy.Send.
type ChannelSendOpts struct {
	// Sender is an optional descriptive label (e.g. agent ID).
	Sender string
	// Type is an optional message type for filtering on receive.
	Type string
	// CorrelationID is an optional correlation key for request/response.
	CorrelationID string
	// Headers is an optional string-string metadata bag.
	Headers map[string]string
}

// ChannelReceiveOpts tweaks a ChannelsProxy.Receive call.
type ChannelReceiveOpts struct {
	// Consumer enables server-tracked cursor mode. Subsequent calls
	// without Since resume from the previous batch's tail.
	Consumer string
	// Since restricts results to messages with seq > since. Default 0.
	Since int64
	// Limit is the max messages returned. Server defaults to 100, max 1000.
	Limit int
	// Peek, when true, returns messages without advancing the consumer cursor.
	Peek bool
}

// ChannelsProxy is the v0.2 typed-message-queue surface for a workspace.
type ChannelsProxy struct {
	ws *WorkspaceClient
}

func newChannelsProxy(ws *WorkspaceClient) *ChannelsProxy { return &ChannelsProxy{ws: ws} }

// Send delivers payload on channel. Channels are created lazily on
// first send.
func (c *ChannelsProxy) Send(ctx context.Context, channel string, payload any, opts ChannelSendOpts) (*ChannelMessage, error) {
	body := map[string]any{"payload": payload}
	if opts.Sender != "" {
		body["sender"] = opts.Sender
	}
	if opts.Type != "" {
		body["type"] = opts.Type
	}
	if opts.CorrelationID != "" {
		body["correlation_id"] = opts.CorrelationID
	}
	if len(opts.Headers) > 0 {
		body["headers"] = opts.Headers
	}
	var msg ChannelMessage
	err := c.ws.http.PostJSON(
		ctx,
		c.basePath()+"/"+EncodePathSegment(channel)+"/send",
		&msg,
		WithJSON(body),
		WithQuery(c.ws.branchQuery()),
		WithNotFoundCode(ErrWorkspaceNotFound.Code),
	)
	if err != nil {
		return nil, err
	}
	return &msg, nil
}

// Receive returns a batch of messages from channel. When opts.Consumer
// is set, the server tracks a cursor so subsequent calls resume.
func (c *ChannelsProxy) Receive(ctx context.Context, channel string, opts ChannelReceiveOpts) ([]ChannelMessage, error) {
	q := c.ws.branchQuery()
	if opts.Consumer != "" {
		q.Set("consumer", opts.Consumer)
	}
	if opts.Since > 0 {
		q.Set("since", strconv.FormatInt(opts.Since, 10))
	}
	if opts.Limit > 0 {
		q.Set("limit", strconv.Itoa(opts.Limit))
	}
	if opts.Peek {
		q.Set("peek", "true")
	}
	var resp struct {
		Messages []ChannelMessage `json:"messages"`
	}
	err := c.ws.http.GetJSON(
		ctx,
		c.basePath()+"/"+EncodePathSegment(channel)+"/receive",
		&resp,
		WithQuery(q),
		WithNotFoundCode(ErrChannelNotFound.Code),
	)
	if err != nil {
		return nil, err
	}
	return resp.Messages, nil
}

// Ack deletes a previously-received message. Pass the message you got
// back from Receive — the channel name comes off the message so a bare
// ID isn't enough to compute the URL.
func (c *ChannelsProxy) Ack(ctx context.Context, msg ChannelMessage) error {
	return c.ws.http.Delete(
		ctx,
		c.basePath()+"/"+EncodePathSegment(msg.Channel)+"/messages/"+EncodePathSegment(msg.ID),
		WithNotFoundCode(ErrMessageNotFound.Code),
	)
}

// List returns every channel on the workspace.
func (c *ChannelsProxy) List(ctx context.Context) ([]Channel, error) {
	var resp struct {
		Channels []Channel `json:"channels"`
	}
	err := c.ws.http.GetJSON(
		ctx,
		c.basePath(),
		&resp,
		WithQuery(c.ws.branchQuery()),
		WithNotFoundCode(ErrWorkspaceNotFound.Code),
	)
	if err != nil {
		return nil, err
	}
	return resp.Channels, nil
}

// Get fetches a single channel by name.
func (c *ChannelsProxy) Get(ctx context.Context, channel string) (*Channel, error) {
	var ch Channel
	err := c.ws.http.GetJSON(
		ctx,
		c.basePath()+"/"+EncodePathSegment(channel),
		&ch,
		WithQuery(c.ws.branchQuery()),
		WithNotFoundCode(ErrChannelNotFound.Code),
	)
	if err != nil {
		return nil, err
	}
	return &ch, nil
}

// Delete removes channel and every message in it. Idempotent — a
// missing channel returns ErrChannelNotFound.
func (c *ChannelsProxy) Delete(ctx context.Context, channel string) error {
	return c.ws.http.Delete(
		ctx,
		c.basePath()+"/"+EncodePathSegment(channel),
		WithNotFoundCode(ErrChannelNotFound.Code),
	)
}

func (c *ChannelsProxy) basePath() string {
	return "/v1/workspaces/" + EncodePathSegment(c.ws.ID()) + "/channels"
}
