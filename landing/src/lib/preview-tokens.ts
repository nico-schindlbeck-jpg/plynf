// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plynf Authors
/**
 * Browser token counting for the savings preview. Mirrors
 * `services/proxy/src/plinth_proxy/tokens.py`: o200k_base (GPT-4o family),
 * counted on the compact, ASCII-escaped JSON serialisation — i.e. exactly
 * what the proxy bills/reports. The encoder (~1 MB gzipped) is loaded
 * lazily on first use so the page itself stays fast.
 */

import { compactJson, type Json } from "./shaper";

type Encoder = (text: string) => number[];

let encodePromise: Promise<Encoder> | null = null;

async function loadEncoder(): Promise<Encoder> {
  if (!encodePromise) {
    encodePromise = import("gpt-tokenizer/encoding/o200k_base").then(
      (m) => m.encode as Encoder,
    );
  }
  return encodePromise;
}

/** Preload the tokenizer (call on user intent, e.g. focus on the form). */
export function warmup(): void {
  void loadEncoder().catch(() => {
    /* fall back to estimate at count time */
  });
}

export interface TokenCount {
  tokens: number;
  /** False if the real tokenizer failed to load and we estimated (~4 chars/token). */
  exact: boolean;
}

export async function countJsonTokens(value: Json): Promise<TokenCount> {
  const serialised = compactJson(value);
  try {
    const encode = await loadEncoder();
    return { tokens: encode(serialised).length, exact: true };
  } catch {
    return { tokens: Math.max(1, Math.floor(serialised.length / 4)), exact: false };
  }
}
