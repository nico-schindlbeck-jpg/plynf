/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 *
 * Internal fetch wrapper.
 *
 * Centralises:
 *   - bearer auth header injection
 *   - timeout via AbortController
 *   - error-envelope parsing → typed errors
 *   - JSON / bytes / void response shapes
 *
 * Nothing in here is part of the public SDK surface; consumers should
 * use the high-level classes in {@link client.ts} / {@link workspace.ts}.
 */

import { errorFromEnvelope, PlinthError } from "./errors.js";
import type { ErrorEnvelope, JsonValue } from "./types.js";

export type QueryValue = string | number | boolean | null | undefined;

export interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "DELETE" | "PATCH";
  /** Path joined onto baseUrl, e.g. `/v1/workspaces`. */
  path: string;
  /** Querystring parameters. `null`/`undefined` values are dropped. */
  query?: Record<string, QueryValue>;
  /** JSON body — mutually exclusive with `bytes`. */
  json?: JsonValue;
  /** Raw body bytes (e.g. file uploads) — mutually exclusive with `json`. */
  bytes?: Uint8Array | ArrayBuffer | string;
  /** Override the Content-Type when sending raw bytes. */
  contentType?: string;
  /** Per-request timeout override in ms. */
  timeoutMs?: number;
  /** AbortSignal to merge with the timeout signal. */
  signal?: AbortSignal;
}

export type ResponseShape = "json" | "bytes" | "void";

export interface HttpClientConfig {
  baseUrl: string;
  apiKey: string;
  defaultTimeoutMs: number;
  fetch: typeof fetch;
}

/**
 * Thin wrapper around `fetch` with auth, timeout, and error mapping.
 *
 * One instance per service base URL — the SDK creates two
 * (workspace + gateway) and keeps them on the {@link Plinth} client.
 */
export class HttpClient {
  private readonly baseUrl: string;
  private readonly apiKey: string;
  private readonly defaultTimeoutMs: number;
  private readonly fetchImpl: typeof fetch;

  constructor(config: HttpClientConfig) {
    // Strip trailing slash so we can confidently concatenate paths.
    this.baseUrl = config.baseUrl.replace(/\/+$/, "");
    this.apiKey = config.apiKey;
    this.defaultTimeoutMs = config.defaultTimeoutMs;
    this.fetchImpl = config.fetch;
  }

  /** Issue a request and decode the body as JSON. */
  async requestJson<T>(opts: RequestOptions): Promise<T> {
    const res = await this.send(opts);
    return (await res.json()) as T;
  }

  /** Issue a request and return the raw body as a Uint8Array. */
  async requestBytes(opts: RequestOptions): Promise<Uint8Array> {
    const res = await this.send(opts);
    const buf = await res.arrayBuffer();
    return new Uint8Array(buf);
  }

  /** Issue a request and discard the body (for 204 responses). */
  async requestVoid(opts: RequestOptions): Promise<void> {
    const res = await this.send(opts);
    // Drain to free the connection — we don't care about the bytes.
    if (res.body) {
      try {
        await res.arrayBuffer();
      } catch {
        // ignore
      }
    }
  }

  private async send(opts: RequestOptions): Promise<Response> {
    const url = this.buildUrl(opts.path, opts.query);
    const headers = new Headers();
    headers.set("Authorization", `Bearer ${this.apiKey}`);
    headers.set("Accept", "application/json, application/octet-stream");

    let body: string | Uint8Array | ArrayBuffer | undefined;
    if (opts.json !== undefined) {
      headers.set("Content-Type", "application/json");
      body = JSON.stringify(opts.json);
    } else if (opts.bytes !== undefined) {
      headers.set("Content-Type", opts.contentType ?? "application/octet-stream");
      body = opts.bytes;
    }

    const controller = new AbortController();
    const timeoutMs = opts.timeoutMs ?? this.defaultTimeoutMs;
    const timer = setTimeout(() => controller.abort(new Error(`Request timed out after ${timeoutMs}ms`)), timeoutMs);

    // Merge external signal with the timeout signal.
    const onExternalAbort = (): void => controller.abort(opts.signal?.reason);
    if (opts.signal) {
      if (opts.signal.aborted) controller.abort(opts.signal.reason);
      else opts.signal.addEventListener("abort", onExternalAbort, { once: true });
    }

    let res: Response;
    try {
      res = await this.fetchImpl(url, {
        method: opts.method ?? "GET",
        headers,
        // `RequestInit.body` accepts BodyInit which includes our union;
        // cast keeps us free of the DOM lib (Node 20 fetch is undici).
        body: body as RequestInit["body"],
        signal: controller.signal,
      });
    } catch (err) {
      if (controller.signal.aborted) {
        throw new PlinthError(
          `Request to ${url} aborted: ${(err as Error).message}`,
          "INTERNAL_ERROR",
        );
      }
      throw new PlinthError(
        `Network error contacting ${url}: ${(err as Error).message}`,
        "INTERNAL_ERROR",
      );
    } finally {
      clearTimeout(timer);
      if (opts.signal) opts.signal.removeEventListener("abort", onExternalAbort);
    }

    if (!res.ok) {
      const envelope = await safeReadErrorEnvelope(res);
      throw errorFromEnvelope(res.status, envelope);
    }
    return res;
  }

  private buildUrl(path: string, query?: Record<string, QueryValue>): string {
    const normalised = path.startsWith("/") ? path : `/${path}`;
    const url = new URL(`${this.baseUrl}${normalised}`);
    if (query) {
      for (const [key, value] of Object.entries(query)) {
        if (value === null || value === undefined) continue;
        url.searchParams.set(key, String(value));
      }
    }
    return url.toString();
  }
}

async function safeReadErrorEnvelope(res: Response): Promise<Partial<ErrorEnvelope> | null> {
  try {
    const text = await res.text();
    if (!text) return null;
    return JSON.parse(text) as Partial<ErrorEnvelope>;
  } catch {
    return null;
  }
}

/** Encode a path segment for use in URLs — preserves `/` for file paths. */
export function encodePath(segment: string): string {
  return segment
    .split("/")
    .map((part) => encodeURIComponent(part))
    .join("/");
}
