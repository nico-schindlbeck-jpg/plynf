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
  /**
   * v1.0 — multi-region. Map of fallback region id → base URL. Tried in
   * iteration order on connection failure / 5xx / 503; also consulted
   * when the primary returns 409 with `X-Plinth-Primary-Region`. Object
   * iteration order matches insertion order, so the operator gets
   * deterministic failover ordering.
   */
  fallbackUrls?: Record<string, string>;
  /**
   * v1.0 — multi-region. The region id of the primary `baseUrl`.
   * Surfaced in failover logs and used as the lookup key when the
   * server's 409 redirect points at our own primary region.
   */
  primaryRegion?: string;
}

/**
 * Thin wrapper around `fetch` with auth, timeout, and error mapping.
 *
 * One instance per service base URL — the SDK creates two
 * (workspace + gateway) and keeps them on the {@link Plinth} client.
 *
 * v1.0 — supports multi-region failover. Pass `fallbackUrls` to enable
 * automatic retry against alternate regions on connection failures or
 * replica redirects (`421 / 409` with `X-Plinth-Primary-Region` and/or
 * `X-Plinth-Primary-URL`).
 *
 * The redirect retry is bounded: each unique base URL is tried at most
 * once per request, so a misconfigured pair of replicas can never loop.
 */
export class HttpClient {
  private readonly baseUrl: string;
  private readonly apiKey: string;
  private readonly defaultTimeoutMs: number;
  private readonly fetchImpl: typeof fetch;
  private readonly fallbackUrls: Record<string, string>;
  private readonly primaryRegion: string | undefined;

  constructor(config: HttpClientConfig) {
    // Strip trailing slash so we can confidently concatenate paths.
    this.baseUrl = config.baseUrl.replace(/\/+$/, "");
    this.apiKey = config.apiKey;
    this.defaultTimeoutMs = config.defaultTimeoutMs;
    this.fetchImpl = config.fetch;
    this.fallbackUrls = {};
    for (const [region, url] of Object.entries(config.fallbackUrls ?? {})) {
      this.fallbackUrls[region] = url.replace(/\/+$/, "");
    }
    this.primaryRegion = config.primaryRegion;
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

  /**
   * Return the ordered list of `[region, baseUrl]` candidates to try.
   *
   * Exposed for the multi-region failover path; intentionally typed
   * `readonly` so callers can't reorder it. Insertion order matches the
   * original `fallbackUrls` config so failover is deterministic.
   */
  candidates(): ReadonlyArray<readonly [string, string]> {
    const out: Array<readonly [string, string]> = [
      [this.primaryRegion ?? "<primary>", this.baseUrl],
    ];
    for (const [region, url] of Object.entries(this.fallbackUrls)) {
      out.push([region, url]);
    }
    return out;
  }

  /**
   * Pick the redirect URL from the replica response headers.
   *
   * Resolution order:
   *   1. `X-Plinth-Primary-URL` if it matches a known candidate URL.
   *      Trusting an arbitrary URL header would let a hostile response
   *      redirect the SDK at an attacker-controlled host, so we only
   *      honour URLs the operator has already configured.
   *   2. `X-Plinth-Primary-Region` looked up in `fallbackUrls`.
   *   3. `X-Plinth-Primary-Region` matching `primaryRegion` →
   *      our own `baseUrl`.
   *
   * Returns `null` if no trusted target can be derived.
   */
  private resolveRedirectTarget(
    primaryRegion: string | undefined,
    primaryUrlHint: string | undefined,
  ): string | null {
    if (primaryUrlHint) {
      const normalized = primaryUrlHint.replace(/\/+$/, "");
      const known = new Set<string>([this.baseUrl, ...Object.values(this.fallbackUrls)]);
      if (known.has(normalized)) return normalized;
    }
    if (!primaryRegion) return null;
    const byRegion = this.fallbackUrls[primaryRegion];
    if (byRegion) return byRegion;
    if (this.primaryRegion === primaryRegion) return this.baseUrl;
    return null;
  }

  private async send(opts: RequestOptions): Promise<Response> {
    const candidates = this.candidates();
    const queue: Array<readonly [string, string]> = [...candidates];
    const attempted = new Set<string>();
    let lastError: Error | null = null;
    let idx = 0;
    while (idx < queue.length) {
      const [region, base] = queue[idx]!;
      idx++;
      if (attempted.has(base)) continue;
      attempted.add(base);
      try {
        const res = await this.sendOne(base, opts);

        // 421 (Misdirected Request) — or 409 for back-compat — with
        // replica-redirect headers: route to the named primary region.
        // ``X-Plinth-Primary-URL`` (preferred) is trusted only if it
        // matches a known candidate URL, so a malicious header can't
        // steer the SDK at an attacker-controlled host.
        if (res.status === 421 || res.status === 409) {
          const primaryRegion = res.headers.get("X-Plinth-Primary-Region")?.trim();
          const primaryUrlHint = res.headers.get("X-Plinth-Primary-URL")?.trim();
          if (primaryRegion || primaryUrlHint) {
            const target = this.resolveRedirectTarget(primaryRegion, primaryUrlHint);
            if (target && !attempted.has(target)) {
              // eslint-disable-next-line no-console
              console.warn(
                `[plinth.sdk.failover] replica_redirect region=${region} ` +
                  `url=${base} -> ${target}`,
              );
              queue.splice(idx, 0, [primaryRegion ?? "<primary>", target] as const);
              // Drain the response so we don't hold the connection.
              try {
                await res.arrayBuffer();
              } catch {
                // ignore
              }
              continue;
            }
          }
        }

        // 5xx / 503: try the next candidate if any.
        if (
          (res.status === 502 || res.status === 503 || res.status === 504) &&
          idx < queue.length
        ) {
          // eslint-disable-next-line no-console
          console.warn(
            `[plinth.sdk.failover] upstream_5xx region=${region} ` +
              `url=${base} status=${res.status}`,
          );
          try {
            await res.arrayBuffer();
          } catch {
            // ignore
          }
          lastError = new PlinthError(
            `upstream ${res.status} from ${base}`,
            "UPSTREAM_DEGRADED",
          );
          continue;
        }

        if (!res.ok) {
          const envelope = await safeReadErrorEnvelope(res);
          throw errorFromEnvelope(res.status, envelope);
        }
        return res;
      } catch (err) {
        if (err instanceof PlinthError && err.code !== "INTERNAL_ERROR") {
          // Typed Plinth errors (4xx etc.) are not retried.
          throw err;
        }
        // eslint-disable-next-line no-console
        console.warn(
          `[plinth.sdk.failover] connection_error region=${region} ` +
            `url=${base} err=${(err as Error).message}`,
        );
        lastError = err as Error;
        continue;
      }
    }
    if (lastError) throw lastError;
    throw new PlinthError("no candidate URLs succeeded", "INTERNAL_ERROR");
  }

  private async sendOne(base: string, opts: RequestOptions): Promise<Response> {
    const url = this.buildUrl(base, opts.path, opts.query);
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
    const timer = setTimeout(
      () => controller.abort(new Error(`Request timed out after ${timeoutMs}ms`)),
      timeoutMs,
    );

    const onExternalAbort = (): void => controller.abort(opts.signal?.reason);
    if (opts.signal) {
      if (opts.signal.aborted) controller.abort(opts.signal.reason);
      else opts.signal.addEventListener("abort", onExternalAbort, { once: true });
    }

    try {
      return await this.fetchImpl(url, {
        method: opts.method ?? "GET",
        headers,
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
  }

  private buildUrl(
    base: string,
    path: string,
    query?: Record<string, QueryValue>,
  ): string {
    const normalised = path.startsWith("/") ? path : `/${path}`;
    const url = new URL(`${base}${normalised}`);
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
