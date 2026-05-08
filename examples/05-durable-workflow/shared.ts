/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 The Plinth Authors
 *
 * Shared helpers — TypeScript counterpart of `shared.py`.
 *
 * Mock LLM and search/fetch sources so the demo runs offline. Used by
 * both `handlers.ts` and `start-workflow.ts`.
 */

export function slugify(text: string): string {
  return text
    .replace(/[^a-zA-Z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .toLowerCase()
    .slice(0, 80);
}

export interface MockSource {
  url: string;
  title: string;
  snippet: string;
}

export function mockSearch(topic: string, k = 5): MockSource[] {
  const base = topic.replace(/\s+/g, "-").toLowerCase();
  const out: MockSource[] = [];
  for (let i = 1; i <= k; i++) {
    out.push({
      url: `mock://${base}/${i}`,
      title: `${capitalise(topic)} Insight #${i}`,
      snippet: `A short excerpt about ${topic} (mock source #${i}).`,
    });
  }
  return out;
}

export function mockFetch(url: string): string {
  return (
    `# Source content for ${url}\n\n` +
    "This is a mock fetched document used by the durable-workflow demo. " +
    "Each source paragraph is intentionally distinct so the synthesise " +
    "step has something to weave together.\n\n" +
    `Body: lorem ipsum about ${url}, with three or four sentences. ` +
    "Ending with a clear takeaway.\n"
  );
}

export function mockExtract(content: string, topic: string): string[] {
  return [
    `Fact about ${topic} from ${content.length} chars of source`,
    `Key driver of ${topic} mentioned in the source`,
    `Risk factor for ${topic} highlighted in the source`,
  ];
}

export function mockSynthesise(
  topic: string,
  factsByUrl: Record<string, string[]>,
): string {
  const lines: string[] = [`# Report: ${topic}`, ""];
  lines.push(`This synthetic report on **${topic}** weaves together facts `);
  const urlCount = Object.keys(factsByUrl).length;
  lines.push(`from ${urlCount} mock sources.\n`);
  for (const [url, facts] of Object.entries(factsByUrl)) {
    lines.push(`## Source: ${url}`);
    for (const f of facts) lines.push(`- ${f}`);
    lines.push("");
  }
  lines.push("## Conclusion\n");
  lines.push(
    `The combined evidence suggests ${topic} is multi-faceted; further ` +
      "investigation is recommended.",
  );
  return lines.join("\n");
}

export interface ClientKwargs {
  workspaceUrl: string;
  gatewayUrl: string;
  apiKey: string;
}

export function makeClientKwargs(): ClientKwargs {
  return {
    workspaceUrl: process.env.PLINTH_WORKSPACE_URL ?? "http://localhost:7421",
    gatewayUrl: process.env.PLINTH_GATEWAY_URL ?? "http://localhost:7422",
    apiKey: process.env.PLINTH_API_KEY ?? "local-dev",
  };
}

export async function servicesAvailable(): Promise<Record<string, boolean>> {
  const targets: Record<string, string> = {
    workspace: process.env.PLINTH_WORKSPACE_URL ?? "http://localhost:7421",
    gateway: process.env.PLINTH_GATEWAY_URL ?? "http://localhost:7422",
  };
  const out: Record<string, boolean> = {};
  for (const [name, url] of Object.entries(targets)) {
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 1000);
      const res = await fetch(`${url}/healthz`, { signal: controller.signal });
      clearTimeout(timer);
      out[name] = res.status === 200;
    } catch {
      out[name] = false;
    }
  }
  return out;
}

function capitalise(s: string): string {
  return s.replace(/\b\w/g, (c) => c.toUpperCase());
}
