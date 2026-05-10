// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

package plinth_test

import (
	"strings"
	"testing"

	"github.com/plinth/sdk-go/plinth"
)

// TestCountTokensEmpty checks the boundary case.
func TestCountTokensEmpty(t *testing.T) {
	if got := plinth.CountTokens(""); got != 0 {
		t.Errorf("CountTokens(\"\") = %d, want 0", got)
	}
}

// TestCountTokensShortText spot-checks the approximate counter against
// an English sentence. The SDK doesn't promise BPE accuracy — only
// that the count is in the right ballpark.
func TestCountTokensShortText(t *testing.T) {
	got := plinth.CountTokens("Hello, world!")
	// "Hello", "world" → ~2 tokens each-ish, plus comma + space + bang.
	// Should be in [4, 12]; our approximator returns ~6.
	if got < 4 || got > 12 {
		t.Errorf("CountTokens = %d, want in [4, 12]", got)
	}
}

// TestCountTokensScalesWithLength sanity-check: longer inputs produce
// strictly more tokens.
func TestCountTokensScalesWithLength(t *testing.T) {
	short := plinth.CountTokens("hello")
	long := plinth.CountTokens(strings.Repeat("hello ", 50))
	if long <= short {
		t.Errorf("long (%d) <= short (%d)", long, short)
	}
}

// TestCountTokensCountsNewlines verifies the algorithm bumps tokens
// for each newline.
func TestCountTokensCountsNewlines(t *testing.T) {
	withoutNL := plinth.CountTokens("a b c d")
	withNL := plinth.CountTokens("a\nb\nc\nd")
	if withNL <= withoutNL {
		t.Errorf("withNL (%d) should exceed withoutNL (%d)", withNL, withoutNL)
	}
}

// TestEstimateCost verifies the price math is in the right order.
func TestEstimateCost(t *testing.T) {
	// 1M input tokens at $3/M → exactly $3.
	if got := plinth.EstimateCost(1_000_000, 0); got < 2.99 || got > 3.01 {
		t.Errorf("EstimateCost(1M, 0) = %v, want ~3.0", got)
	}
	// Output tokens are 5x more expensive than input.
	in := plinth.EstimateCost(1000, 0)
	out := plinth.EstimateCost(0, 1000)
	if out < in*4.5 || out > in*5.5 {
		t.Errorf("output price ratio = %v, want ~5x input", out/in)
	}
}

// TestPlinthClientCountTokens covers the method on *Plinth used by
// callers who don't want to reach for the package function.
func TestPlinthClientCountTokens(t *testing.T) {
	c, err := plinth.New(plinth.Config{APIKey: "x"})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	if c.CountTokens("hello world") < 2 {
		t.Error("CountTokens too low for two words")
	}
	if c.EstimateCost(1000, 0) <= 0 {
		t.Error("EstimateCost returned <= 0 for non-zero tokens")
	}
}
