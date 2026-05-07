/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 *
 * Offline token counting and Sonnet cost estimation.
 *
 * Mirrors `plinth.tokens` in the Python SDK:
 *
 *   * {@link count} — exact `cl100k_base` BPE counts via `gpt-tokenizer`.
 *   * {@link estimateCost} — USD at Sonnet pricing (constants below).
 *
 * The encoder is loaded lazily (and cached) so importing the SDK doesn't
 * pay the BPE cost up front. Empty strings short-circuit to `0`.
 *
 * Why `gpt-tokenizer`? It is the smallest pure-JS implementation of the
 * `cl100k_base` encoding (~150 KB) — the closest publicly available BPE
 * to Anthropic's tokenizer. If it is ever missing at runtime (e.g. an
 * end-user disabled the dep), {@link count} falls back to a `words×1.3`
 * heuristic and logs a one-shot warning, so callers always get a number.
 */

let cachedEncode: ((text: string) => number[]) | null | undefined;
let warned = false;

/** USD per 1M Anthropic Claude Sonnet input tokens. */
export const SONNET_INPUT_USD_PER_MTOK = 3.0;

/** USD per 1M Anthropic Claude Sonnet output tokens. */
export const SONNET_OUTPUT_USD_PER_MTOK = 15.0;

/** Standardised tiktoken encoding name we approximate against. */
export const ENCODING_NAME = "cl100k_base";

/**
 * Lazily resolve the BPE encoder.
 *
 * `gpt-tokenizer` exports a default `encode` for `cl100k_base`. We swallow
 * the import error and fall through to a heuristic so the SDK keeps
 * working in environments where the dep was tree-shaken or pruned.
 */
async function loadEncoder(): Promise<((text: string) => number[]) | null> {
  if (cachedEncode !== undefined) return cachedEncode;
  try {
    // Dynamic import keeps the SDK's eager surface tiny.
    const mod = (await import("gpt-tokenizer")) as { encode?: (text: string) => number[] };
    if (typeof mod.encode === "function") {
      cachedEncode = mod.encode;
      return cachedEncode;
    }
    cachedEncode = null;
    return null;
  } catch {
    cachedEncode = null;
    return null;
  }
}

/**
 * Count tokens in `text` using `cl100k_base` BPE.
 *
 * Returns `0` for empty input. If the BPE encoder is not available (the
 * `gpt-tokenizer` runtime dep is missing), falls back to a `words × 1.3`
 * heuristic and logs a one-shot warning. The heuristic is intentionally
 * coarse; budget callers should treat it as approximate.
 */
export async function count(text: string): Promise<number> {
  if (!text) return 0;
  const enc = await loadEncoder();
  if (enc) return enc(text).length;
  if (!warned) {
    warned = true;
    // eslint-disable-next-line no-console
    console.warn(
      "@plinth/sdk: gpt-tokenizer not available — countTokens is using a " +
        "words×1.3 heuristic. Install `gpt-tokenizer` for exact counts.",
    );
  }
  return heuristicCount(text);
}

/**
 * Synchronous heuristic — `words × 1.3`, rounded up.
 *
 * Exposed so tests can compare against a deterministic baseline without
 * patching the dynamic import path. Production code should prefer the
 * async {@link count}.
 */
export function heuristicCount(text: string): number {
  if (!text) return 0;
  const words = text.trim().split(/\s+/).filter(Boolean).length;
  return Math.ceil(words * 1.3);
}

/**
 * Estimate the USD cost of a Sonnet request.
 *
 * Constants are exposed at module scope ({@link SONNET_INPUT_USD_PER_MTOK},
 * {@link SONNET_OUTPUT_USD_PER_MTOK}) so they're trivial to refresh when
 * Anthropic publishes new pricing.
 */
export function estimateCost(promptTokens: number, completionTokens = 0): number {
  if (promptTokens < 0 || completionTokens < 0) {
    throw new RangeError("Token counts must be non-negative");
  }
  const inputCost = (promptTokens * SONNET_INPUT_USD_PER_MTOK) / 1_000_000;
  const outputCost = (completionTokens * SONNET_OUTPUT_USD_PER_MTOK) / 1_000_000;
  return inputCost + outputCost;
}

/**
 * Test-only hook: reset the cached encoder so a fresh `count` call
 * re-resolves the dynamic import. Used by the test suite to exercise both
 * the BPE and heuristic branches.
 *
 * @internal
 */
export function _resetForTests(): void {
  cachedEncode = undefined;
  warned = false;
}
