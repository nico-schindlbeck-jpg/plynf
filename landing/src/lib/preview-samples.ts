// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plynf Authors
/**
 * Realistic, deliberately bloated sample tool responses for the savings
 * preview — one per bundled connector. Keep-lists mirror the shipped
 * default policies in `services/proxy/src/plinth_proxy/policies/`.
 *
 * Data lives in `preview-samples.json` (single source of truth — the
 * parity-fixture generator `scripts/gen_shaper_fixtures.py` reads the
 * same file and runs it through the real Python engine).
 */

import type { Json } from "./shaper";
import samples from "./preview-samples.json";

export interface PreviewSample {
  id: string;
  label: string;
  tool: string;
  /** Default keep-list (from the shipped policy). */
  keepList: string[];
  raw: Json;
}

export const SAMPLES: PreviewSample[] = samples as unknown as PreviewSample[];
