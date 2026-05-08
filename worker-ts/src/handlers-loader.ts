/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 *
 * Dynamic handlers-module loader.
 *
 * The CLI accepts `--handlers-module <spec>`. `<spec>` may be:
 *
 *   - a relative file path (`./handlers.js`),
 *   - an absolute file path (`/srv/app/handlers.js`),
 *   - a bare package name (`my-app/handlers`).
 *
 * The module is expected to export a `register(runtime, client)`
 * function that populates the runtime. We chose this over a
 * decorator-driven side-effect API because ESM's stricter import
 * semantics make import-time side effects unreliable across bundlers
 * and runtimes — `register` is explicit and trivial to test.
 */

import { pathToFileURL } from "node:url";
import { isAbsolute, resolve } from "node:path";

import type { Plinth } from "@plinth/sdk";

import type { WorkflowRuntime } from "./runtime.js";

/** Shape the handlers module is expected to export. */
export interface HandlersModule {
  register(runtime: WorkflowRuntime, client: Plinth): void | Promise<void>;
}

/**
 * Load `modulePath` and call its `register(runtime, client)` export.
 *
 * Throws if the import fails or if the module does not export a
 * `register` function.
 */
export async function loadHandlers(
  modulePath: string,
  runtime: WorkflowRuntime,
  client: Plinth,
): Promise<void> {
  const target = resolveModulePath(modulePath);
  const mod = (await import(target)) as Partial<HandlersModule>;
  if (typeof mod.register !== "function") {
    throw new Error(
      `handlers module ${JSON.stringify(modulePath)} must export ` +
        `a 'register(runtime, client)' function`,
    );
  }
  await mod.register(runtime, client);
}

/**
 * Translate a user-supplied module spec into something `import()`
 * accepts.
 *
 * Relative + absolute file paths must be turned into `file://` URLs;
 * bare package names pass through unchanged so Node's resolver can
 * walk `node_modules`.
 */
export function resolveModulePath(spec: string): string {
  // Already a URL — use as-is.
  if (/^[a-z]+:\/\//i.test(spec)) return spec;
  // Looks like a file path.
  if (
    spec.startsWith("./") ||
    spec.startsWith("../") ||
    spec.startsWith(".\\") ||
    spec.startsWith("..\\") ||
    isAbsolute(spec)
  ) {
    const abs = isAbsolute(spec) ? spec : resolve(process.cwd(), spec);
    return pathToFileURL(abs).href;
  }
  // Bare specifier — Node resolves through node_modules.
  return spec;
}
