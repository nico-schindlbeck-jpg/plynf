// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plinth Authors

package plinth

import (
	"strings"
	"unicode"
)

// Token-counting and cost-estimation helpers. Mirrors
// sdk/python/src/plinth/tokens.py and sdk/typescript/src/tokens.ts.
//
// v0.1 of the Go SDK uses a deliberately lightweight approximation:
// full tiktoken / gpt-tokenizer would pull in a multi-MB BPE table and
// mean a non-stdlib dependency. The approximation:
//
//   1. Splits text into "words" (runs of letters/digits) and punctuation.
//   2. For each word, estimates ceil(len(word) / 4) tokens — matches
//      cl100k's average byte-pair encoding density on English / code.
//   3. Counts every punctuation rune as a single token.
//   4. Treats whitespace runs (newlines, multiple spaces) as one token
//      each — newlines are often their own BPE token in cl100k.
//
// The result is within ~5–10% of cl100k_base on typical English /
// code prompts. For exact accounting (billing, hard token caps),
// callers should reach for a full BPE implementation.

// SonnetInputUSDPerMTok is the Sonnet 4.x input price per million
// tokens. Mirrors the Python SDK's constants.
const SonnetInputUSDPerMTok = 3.0

// SonnetOutputUSDPerMTok is the Sonnet 4.x output price per million
// tokens.
const SonnetOutputUSDPerMTok = 15.0

// CountTokens returns an approximate cl100k token count for text.
//
// See the package doc comment above for the algorithm and accuracy
// caveats. Returns 0 for the empty string.
func CountTokens(text string) int {
	if text == "" {
		return 0
	}
	tokens := 0
	var word strings.Builder

	flushWord := func() {
		if word.Len() == 0 {
			return
		}
		// Average BPE token length on cl100k_base is ~4 chars for
		// English; cap at 1 for very short words.
		n := (word.Len() + 3) / 4
		if n < 1 {
			n = 1
		}
		tokens += n
		word.Reset()
	}

	inWhitespace := false
	for _, r := range text {
		switch {
		case unicode.IsSpace(r):
			flushWord()
			if !inWhitespace {
				// One token per whitespace run.
				tokens++
				inWhitespace = true
			}
			// Newlines are usually their own BPE token in cl100k —
			// add an extra so multi-line prompts don't undercount.
			if r == '\n' {
				tokens++
			}
		case unicode.IsLetter(r) || unicode.IsDigit(r):
			word.WriteRune(r)
			inWhitespace = false
		default:
			// Punctuation, symbols, control chars: each gets one token.
			flushWord()
			tokens++
			inWhitespace = false
		}
	}
	flushWord()
	if tokens == 0 {
		// Defensive — non-empty input should always produce ≥1 token.
		return 1
	}
	return tokens
}

// EstimateCost returns a USD estimate for a Sonnet-class request given
// its input and output token counts. Pass 0 for outputTokens to get
// the prompt-only cost.
func EstimateCost(inputTokens, outputTokens int) float64 {
	in := float64(inputTokens) * SonnetInputUSDPerMTok / 1_000_000.0
	out := float64(outputTokens) * SonnetOutputUSDPerMTok / 1_000_000.0
	return in + out
}
