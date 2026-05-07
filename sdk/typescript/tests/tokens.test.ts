/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 */

import { describe, expect, it } from "vitest";

import {
  ENCODING_NAME,
  SONNET_INPUT_USD_PER_MTOK,
  SONNET_OUTPUT_USD_PER_MTOK,
  countTokens,
  estimateCost,
  heuristicCount,
} from "../src/index.js";

describe("tokens.count / heuristicCount", () => {
  it("returns 0 for empty input", async () => {
    await expect(countTokens("")).resolves.toBe(0);
    expect(heuristicCount("")).toBe(0);
  });

  it("returns a positive integer for non-empty input", async () => {
    const n = await countTokens("Hello world, this is a test of the tokenizer.");
    expect(Number.isInteger(n)).toBe(true);
    expect(n).toBeGreaterThan(0);
  });

  it("heuristicCount is words×1.3 rounded up", () => {
    // 10 words × 1.3 = 13
    const ten = "one two three four five six seven eight nine ten";
    expect(heuristicCount(ten)).toBe(13);
    // Lots of whitespace collapses
    expect(heuristicCount("  alpha   beta  ")).toBe(3);
  });

  it("countTokens is deterministic for the same input", async () => {
    const sample = "renewable energy in 2026";
    const a = await countTokens(sample);
    const b = await countTokens(sample);
    expect(a).toBe(b);
  });

  it("countTokens handles unicode without crashing", async () => {
    const n = await countTokens("café — naïve – fiancée");
    expect(n).toBeGreaterThan(0);
  });
});

describe("tokens.estimateCost", () => {
  it("computes Sonnet pricing correctly", () => {
    // 1M prompt + 0 completion = 1 × SONNET_INPUT_USD_PER_MTOK
    expect(estimateCost(1_000_000, 0)).toBeCloseTo(SONNET_INPUT_USD_PER_MTOK, 6);
    // 0 prompt + 1M completion = SONNET_OUTPUT_USD_PER_MTOK
    expect(estimateCost(0, 1_000_000)).toBeCloseTo(SONNET_OUTPUT_USD_PER_MTOK, 6);
    // Mixed
    expect(estimateCost(1000, 500)).toBeCloseTo(
      (1000 * SONNET_INPUT_USD_PER_MTOK + 500 * SONNET_OUTPUT_USD_PER_MTOK) / 1_000_000,
      9,
    );
  });

  it("rejects negative counts", () => {
    expect(() => estimateCost(-1)).toThrow(RangeError);
    expect(() => estimateCost(0, -1)).toThrow(RangeError);
  });
});

describe("tokens module constants", () => {
  it("exports the expected encoding name", () => {
    expect(ENCODING_NAME).toBe("cl100k_base");
  });
});
