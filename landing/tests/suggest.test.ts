// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 The Plynf Authors
/**
 * Tests for the client-side auto keep-list suggestion (suggest.ts).
 * Run: npm test
 */

import test from "node:test";
import assert from "node:assert/strict";

import { suggestKeepList, renderPolicyYaml } from "../src/lib/suggest";
import { shape } from "../src/lib/shaper";

const ORDER = {
  orderId: "ORD-2024-78421",
  customerName: "Acme GmbH",
  status: "in_transit",
  trackingNumber: "1Z999AA10123456784",
  totalAmount: 1249.99,
  internalRoutingCode: "WH3-X-441",
  paymentProcessorRef: "stripe_pi_3NqK8aLkdIwHu7ix1JZQbR2v",
  fraudScore: 0.02,
  warehouseNotes: null,
  giftMessage: "",
  createdAt: "2026-06-01T10:02:11Z",
  etag: 'W/"order-78421-v7"',
  _version: 7,
  lineItems: [{ sku: "WID-PRO-2026", qty: 3, warehouseBin: "A-12-3", supplierRef: "SUP-441" }],
};

test("keeps business fields, drops plumbing and metadata", () => {
  const s = suggestKeepList(ORDER as never);

  assert.ok(s.keepList.includes("status"), "status must be kept");
  assert.ok(s.keepList.includes("customerName"), "customerName must be kept");
  assert.ok(s.keepList.includes("totalAmount"), "totalAmount must be kept");
  assert.ok(s.keepList.includes("trackingNumber"), "trackingNumber must be kept");
  assert.ok(s.keepList.includes("orderId"), "primary id must be kept");
  assert.ok(s.keepList.includes("lineItems.sku"), "nested sku must be kept");

  assert.ok(!s.keepList.includes("internalRoutingCode"), "internal plumbing must be dropped");
  assert.ok(!s.keepList.includes("paymentProcessorRef"), "processor ref must be dropped");
  assert.ok(!s.keepList.includes("createdAt"), "audit metadata must be dropped");
  assert.ok(!s.keepList.includes("etag"), "etag must be dropped");
  assert.ok(!s.keepList.includes("lineItems.warehouseBin"), "warehouse bin must be dropped");
  assert.ok(!s.usedFallback);
});

test("suggested keep-list actually shrinks the payload through the shaper", () => {
  const s = suggestKeepList(ORDER as never);
  const result = shape(ORDER as never, { keepList: s.keepList });
  assert.equal(result.fallback, false, "suggestion must never trigger the safe fallback");
  const shaped = result.shaped as Record<string, unknown>;
  assert.equal(shaped["status"], "in_transit");
  assert.ok(!("internalRoutingCode" in shaped));
  assert.ok(!("paymentProcessorRef" in shaped));
});

test("falls back to short scalars instead of suggesting a blanking policy", () => {
  const opaque = { x1: "zz", x2: "yy", q9: 4 };
  const s = suggestKeepList(opaque as never);
  assert.ok(s.usedFallback, "fallback expected for unrecognisable schemas");
  assert.ok(s.keepList.length >= 3, "fallback keeps the short scalar fields");
});

test("foreign-key ids are dropped, primary id survives", () => {
  const sf = {
    Id: "0068b00001PqRsTUAV",
    OwnerId: "0058b00000GxTqPAAV",
    AccountId: "0018b00002LmNoPQR2",
    StageName: "Negotiation/Review",
    Amount: 184000.5,
  };
  const s = suggestKeepList(sf as never);
  assert.ok(s.keepList.includes("Id"));
  assert.ok(s.keepList.includes("StageName"));
  assert.ok(s.keepList.includes("Amount"));
  assert.ok(!s.keepList.includes("OwnerId"));
  assert.ok(!s.keepList.includes("AccountId"));
});

test("renderPolicyYaml emits a valid-looking connector policy", () => {
  const yaml = renderPolicyYaml("get_order", ["order_id", "status"]);
  assert.ok(yaml.includes("connector: my-connector"));
  assert.ok(yaml.includes("strip_metadata: true"));
  assert.ok(yaml.includes("drop_empty_fields: true"));
  assert.ok(yaml.includes("  get_order:"));
  assert.ok(yaml.includes("      - order_id"));
  assert.ok(yaml.includes("      - status"));
  // Empty tool name falls back to a sane default.
  assert.ok(renderPolicyYaml("  ", ["a"]).includes("  my_tool:"));
});
