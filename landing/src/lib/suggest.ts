// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plynf Authors
/**
 * Auto keep-list suggestion (client-side prototype of P1.1).
 *
 * Given a raw tool response, propose an `allow_fields` keep-list a human
 * can review — deterministic heuristics, no model call, same philosophy
 * as the proxy: *suggest, never silently auto-apply*.
 *
 * The heuristic has three tiers:
 *   1. hard-drop  — audit metadata, internal/sync plumbing, opaque refs,
 *                   hash-/token-like values, API URLs, foreign-key ids
 *   2. keep       — business vocabulary (status, amount, name, dates …)
 *                   and short human-readable strings
 *   3. fallback   — if the suggestion would keep almost nothing, keep all
 *                   short scalar fields instead (never suggest a blanking
 *                   policy; mirrors the engine's safe-fallback spirit)
 */

import { norm, type Json } from "./shaper";

// Mirrors policy_engine._DEFAULT_METADATA_FIELDS (normalised).
const METADATA = new Set(
  [
    "created_at", "createdAt", "CreatedDate", "created_by", "createdBy",
    "updated_at", "updatedAt", "LastModifiedDate", "modified_at",
    "modifiedAt", "modified_by", "modifiedBy", "SystemModstamp",
    "LastViewedDate", "LastReferencedDate", "etag", "_version", "version",
    "rev", "_rev", "attributes",
  ].map(norm),
);

/** Business vocabulary — if a normalised field name CONTAINS one of these
 *  tokens, it is very likely the answer to an agent's question. */
const KEEP_TOKENS = [
  "status", "state", "stage", "name", "title", "company", "amount",
  "total", "price", "revenue", "quantity", "qty", "count", "summary",
  "subject", "text", "message", "description", "carrier", "tracking",
  "delivery", "due", "closedate", "date", "rating", "probability",
  "nextstep", "tier", "industry", "employees", "website", "city",
  "country", "currency", "sku", "user", "owner", "type",
];

/** Plumbing vocabulary — drop regardless of value. */
const DROP_TOKENS = [
  "internal", "sync", "legacy", "fingerprint", "token", "secret",
  "checksum", "etag", "guid", "uuid", "cursor", "pushcount", "fiscal",
  "jigsaw", "duns", "naics", "sic", "geocode", "latitude", "longitude",
  "photourl", "clientmsgid", "blockid", "recordtype", "pricebook",
  "masterrecord", "historyid", "audittrail", "auditlog", "metadata",
  "warehousebin", "taxcode", "taxbreakdown", "routingcode", "processorref",
  "supplierref", "fraudscore", "fraudvendor", "marketingsegments",
  "marketingconsent", "campaigntouchpoints", "deletedflag", "isdeleted",
];

const looksLikeId = (n: string) => n === "id" || n.endsWith("id");
// Opaque machine string: long, no spaces, no slashes (paths/text use those),
// and contains digits — "Negotiation/Review" must NOT match.
const looksLikeHash = (v: string) =>
  /^[A-Za-z0-9_+=-]{18,}$/.test(v) && /\d.*\d/.test(v);
const looksLikeApiPath = (v: string) => /^(\/|https?:\/\/)/.test(v);

export interface SuggestedField {
  path: string;
  reason: string;
  keep: boolean;
}

export interface Suggestion {
  keepList: string[];
  fields: SuggestedField[];
  usedFallback: boolean;
}

interface Leaf {
  path: string[]; // original-case segments, list indices collapsed
  value: Json;
}

function collectLeaves(value: Json, path: string[] = [], out: Leaf[] = [], depth = 0): Leaf[] {
  if (depth > 6) return out;
  if (value !== null && typeof value === "object" && !Array.isArray(value)) {
    for (const [k, v] of Object.entries(value)) {
      collectLeaves(v, [...path, k], out, depth + 1);
    }
    return out;
  }
  if (Array.isArray(value)) {
    // Sample the first element — schema, not data, is what we suggest on.
    if (value.length > 0) collectLeaves(value[0], path, out, depth + 1);
    return out;
  }
  out.push({ path, value });
  return out;
}

function classify(leaf: Leaf, isFirstId: boolean): SuggestedField {
  const dotted = leaf.path.join(".");
  const last = norm(leaf.path[leaf.path.length - 1] ?? "");
  const whole = norm(dotted);
  const v = leaf.value;

  if (METADATA.has(last)) {
    return { path: dotted, keep: false, reason: "audit metadata" };
  }
  if (v === null || v === "") {
    return { path: dotted, keep: false, reason: "empty — lossless layer removes it anyway" };
  }
  if (DROP_TOKENS.some((t) => whole.includes(t))) {
    return { path: dotted, keep: false, reason: "internal plumbing" };
  }
  if (looksLikeId(last)) {
    if (isFirstId) {
      return { path: dotted, keep: true, reason: "primary identifier" };
    }
    return { path: dotted, keep: false, reason: "foreign-key reference" };
  }
  // Name-based intent beats value shape: a field CALLED tracking_number /
  // stage_name is business signal even when its VALUE looks machine-y.
  if (KEEP_TOKENS.some((t) => last.includes(t))) {
    return { path: dotted, keep: true, reason: "business field" };
  }
  if (typeof v === "string" && looksLikeApiPath(v)) {
    return { path: dotted, keep: false, reason: "API url/path" };
  }
  if (typeof v === "string" && looksLikeHash(v)) {
    return { path: dotted, keep: false, reason: "opaque token/hash" };
  }
  if (typeof v === "number" || typeof v === "boolean") {
    return { path: dotted, keep: false, reason: "unrecognised scalar — review" };
  }
  if (typeof v === "string" && v.length <= 80) {
    // Short human-readable string (contains a space or looks like a word).
    if (/\s/.test(v) || /^[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ .,&-]*$/.test(v)) {
      return { path: dotted, keep: true, reason: "human-readable value" };
    }
  }
  return { path: dotted, keep: false, reason: "unrecognised — review" };
}

export function suggestKeepList(raw: Json): Suggestion {
  const leaves = collectLeaves(raw);

  // First id-ish field (document order) is the primary identifier.
  let firstIdPath: string | null = null;
  for (const leaf of leaves) {
    const last = norm(leaf.path[leaf.path.length - 1] ?? "");
    if (looksLikeId(last) && !METADATA.has(last)) {
      const whole = norm(leaf.path.join("."));
      if (!DROP_TOKENS.some((t) => whole.includes(t))) {
        firstIdPath = leaf.path.join(".");
        break;
      }
    }
  }

  const seen = new Set<string>();
  const fields: SuggestedField[] = [];
  for (const leaf of leaves) {
    const dotted = leaf.path.join(".");
    if (seen.has(dotted)) continue;
    seen.add(dotted);
    fields.push(classify(leaf, dotted === firstIdPath));
  }

  let keepList = fields.filter((f) => f.keep).map((f) => f.path);
  let usedFallback = false;

  // Never suggest a blanking policy: if (almost) nothing matched, keep all
  // short scalar fields and let the operator trim.
  if (keepList.length < 3) {
    usedFallback = true;
    keepList = fields
      .filter((f) => {
        const leaf = leaves.find((l) => l.path.join(".") === f.path);
        const v = leaf?.value;
        return v !== null && v !== "" && String(v).length <= 80;
      })
      .map((f) => f.path);
  }

  return { keepList, fields, usedFallback };
}

/** Render a ready-to-use connector policy for the proxy. */
export function renderPolicyYaml(tool: string, keepList: string[]): string {
  const safeTool = tool.trim() || "my_tool";
  const lines = [
    "# Plynf connector policy — generated by the savings preview.",
    "# Review the keep-list before shipping. Lossless minimisation",
    "# (strip_metadata + drop_empty_fields) is safe everywhere.",
    "connector: my-connector",
    "version: 1",
    "",
    "defaults:",
    "  strip_metadata: true",
    "  drop_empty_fields: true",
    "",
    "tools:",
    `  ${safeTool}:`,
    "    allow_fields:",
    ...keepList.map((f) => `      - ${f}`),
  ];
  return lines.join("\n") + "\n";
}
