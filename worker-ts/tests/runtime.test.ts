/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 */

import { describe, expect, it } from "vitest";

import { NoHandlerError } from "@plinth/sdk";

import { WorkflowRuntime, type HandlerContext } from "../src/index.js";

function fakeCtx(): HandlerContext {
  // Tests only ever read `ctx.step.input` / `ctx.workerId`, so we cast
  // the bare-minimum stub to HandlerContext rather than booting a full
  // SDK chain.
  return {
    workerId: "worker_test",
    step: { id: "step_1", name: "search", input: { topic: "x" } },
  } as unknown as HandlerContext;
}

describe("WorkflowRuntime — registration", () => {
  it("registers and dispatches a sync handler", async () => {
    const rt = new WorkflowRuntime();
    rt.register("research", "search", () => ({ ok: true }));
    expect(rt.size()).toBe(1);
    const result = await rt.dispatch("research", "search", fakeCtx());
    expect(result).toEqual({ ok: true });
  });

  it("registers and dispatches an async handler", async () => {
    const rt = new WorkflowRuntime();
    rt.register("research", "search", async (ctx) => {
      const input = ctx.step.input as { topic: string };
      return { topic: input.topic, async: true };
    });
    const result = (await rt.dispatch("research", "search", fakeCtx())) as {
      topic: string;
      async: boolean;
    };
    expect(result.topic).toBe("x");
    expect(result.async).toBe(true);
  });

  it("throws on duplicate registration", () => {
    const rt = new WorkflowRuntime();
    rt.register("a", "b", () => null);
    expect(() => rt.register("a", "b", () => null)).toThrow(/already registered/);
  });

  it("rejects empty workflow or step names", () => {
    const rt = new WorkflowRuntime();
    expect(() => rt.register("", "x", () => null)).toThrow();
    expect(() => rt.register("x", "", () => null)).toThrow();
  });

  it("dispatch throws NoHandlerError for missing handler", async () => {
    const rt = new WorkflowRuntime();
    await expect(rt.dispatch("missing", "step", fakeCtx())).rejects.toBeInstanceOf(
      NoHandlerError,
    );
  });

  it("NoHandlerError carries the offending key in details", async () => {
    const rt = new WorkflowRuntime();
    rt.register("a", "b", () => null);
    try {
      await rt.dispatch("missing", "step", fakeCtx());
      expect.fail("expected NoHandlerError");
    } catch (err) {
      expect(err).toBeInstanceOf(NoHandlerError);
      const e = err as NoHandlerError;
      expect(e.details).toMatchObject({ workflow: "missing", step: "step" });
    }
  });

  it("handler() returns the function unchanged so users can name it", () => {
    const rt = new WorkflowRuntime();
    const fn = rt.handler("a", "b")(async () => "result");
    expect(typeof fn).toBe("function");
    expect(rt.has("a", "b")).toBe(true);
  });

  it("get() returns the registered function and undefined for misses", () => {
    const rt = new WorkflowRuntime();
    const fn = (): null => null;
    rt.register("a", "b", fn);
    expect(rt.get("a", "b")).toBe(fn);
    expect(rt.get("a", "z")).toBeUndefined();
  });

  it("list() returns all registered keys", () => {
    const rt = new WorkflowRuntime();
    rt.register("research", "search", () => null);
    rt.register("research", "fetch", () => null);
    rt.register("writer", "draft", () => null);
    const keys = rt.list();
    expect(keys).toHaveLength(3);
    expect(keys).toContainEqual({ workflow: "research", step: "search" });
    expect(keys).toContainEqual({ workflow: "research", step: "fetch" });
    expect(keys).toContainEqual({ workflow: "writer", step: "draft" });
  });

  it("size() and has() agree with list()", () => {
    const rt = new WorkflowRuntime();
    expect(rt.size()).toBe(0);
    expect(rt.has("a", "b")).toBe(false);
    rt.register("a", "b", () => null);
    expect(rt.size()).toBe(1);
    expect(rt.has("a", "b")).toBe(true);
    expect(rt.has("a", "z")).toBe(false);
  });

  it("propagates exceptions from handlers without wrapping", async () => {
    const rt = new WorkflowRuntime();
    rt.register("a", "b", () => {
      throw new Error("synthetic boom");
    });
    await expect(rt.dispatch("a", "b", fakeCtx())).rejects.toThrow(/synthetic boom/);
  });

  it("handler keys keep workflow and step names distinct", () => {
    const rt = new WorkflowRuntime();
    rt.register("a", "b", () => "ab");
    rt.register("a-b", "c", () => "abc");
    expect(rt.get("a", "b")).not.toBe(rt.get("a-b", "c"));
  });
});
