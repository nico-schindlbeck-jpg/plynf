// SPDX-License-Identifier: Apache-2.0
// App-mention handler: "@Plynf what is the status of order #12345?"
//
// Inference strategy is intentionally simple — keyword-match the user's
// question to one of the registered tools, fetch the shaped data, then
// either:
//   (a) PLYNF_MODEL is unset → reply with a compact JSON summary
//   (b) PLYNF_MODEL is set    → ask the LLM (via Plynf chat-completions)
//                               to draft a one-paragraph human reply
//
// The compact path keeps the bot useful in workspaces without a connected
// LLM credit pool. The LLM path makes it feel like a real agent.

import { shapeTool, chat, listTools, PlynfError } from '../plynf-client.js';

const HEURISTICS = [
  { keywords: ['order', 'bestellung', 'shipment'], tool: 'get_order', extract: extractOrderId },
  { keywords: ['lead'], tool: 'get_lead', extract: extractLeadId },
  { keywords: ['account', 'kunde'], tool: 'get_account', extract: extractAccountId },
  { keywords: ['opportunity', 'deal'], tool: 'get_opportunity', extract: extractOpportunityId },
];

export function registerMention(app, cfg) {
  app.event('app_mention', async ({ event, say, client, logger }) => {
    const text = cleanMention(event.text);
    if (!text) {
      await say({
        thread_ts: event.thread_ts ?? event.ts,
        text:
          `Hi! I can fetch & shape tool responses from your business systems. ` +
          `Try: \`@plynf what is the status of order #12345?\` or \`/plynf-fetch ` +
          `<tool> {json}\`.\n\nKnown tools: ${listTools().join(', ')}`,
      });
      return;
    }

    const picked = pickTool(text);
    if (!picked) {
      await say({
        thread_ts: event.thread_ts ?? event.ts,
        text:
          `I couldn't tell which tool to call. Mention an order, lead, account or ` +
          `opportunity by id — or use the slash command \`/plynf-fetch\` directly.`,
      });
      return;
    }

    try {
      const shaped = await shapeTool({
        baseUrl: cfg.plynfUrl,
        apiKey: cfg.plynfApiKey,
        tool: picked.tool,
        args: picked.args,
        agentId: `slack:${event.user}`,
        workflowId: `mention:${event.channel}`,
      });

      let reply;
      if (cfg.plynfModel) {
        reply = await chat({
          baseUrl: cfg.plynfUrl,
          apiKey: cfg.plynfApiKey,
          model: cfg.plynfModel,
          systemPrompt:
            'You are a Slack assistant. Use ONLY the shaped tool response to ' +
            'answer the user. One short paragraph, friendly tone.',
          userPrompt: text,
          toolJson: shaped.result,
        });
      } else {
        reply = renderCompact(picked.tool, shaped);
      }

      await say({
        thread_ts: event.thread_ts ?? event.ts,
        text: reply,
      });
    } catch (err) {
      logger?.warn?.(err);
      if (err instanceof PlynfError && err.status === 402) {
        await say({
          thread_ts: event.thread_ts ?? event.ts,
          text: `:warning: Plynf tier limit reached. ${err.detail}`,
        });
      } else {
        await say({
          thread_ts: event.thread_ts ?? event.ts,
          text: `:x: Plynf request failed: ${err.message}`,
        });
      }
    }
  });
}

function cleanMention(text) {
  // Strip the leading "<@U…>" bot mention.
  return (text || '').replace(/^<@[A-Z0-9]+>\s*/, '').trim();
}

function pickTool(text) {
  const lower = text.toLowerCase();
  for (const h of HEURISTICS) {
    if (h.keywords.some((k) => lower.includes(k))) {
      const args = h.extract(text);
      if (args !== null) return { tool: h.tool, args };
    }
  }
  return null;
}

function extractOrderId(text) {
  const m = text.match(/#?(\d{4,})/);
  return m ? { order_id: m[1] } : null;
}
function extractLeadId(text) {
  const m = text.match(/(00Q[A-Z0-9]{12,})/i);
  return m ? { id: m[1] } : null;
}
function extractAccountId(text) {
  const m = text.match(/(001[A-Z0-9]{12,})/i);
  return m ? { id: m[1] } : null;
}
function extractOpportunityId(text) {
  const m = text.match(/(006[A-Z0-9]{12,})/i);
  return m ? { id: m[1] } : null;
}

function renderCompact(tool, shaped) {
  const r = shaped.result ?? {};
  const savings = shaped.savings ?? {};
  const pct =
    typeof savings.savings_pct === 'number'
      ? (savings.savings_pct * 100).toFixed(0) + '%'
      : '?';
  const head = `*${tool}* · saved ${pct} tokens`;
  const lines = Object.entries(r)
    .slice(0, 10)
    .map(([k, v]) => `• *${k}*: ${formatValue(v)}`);
  return `${head}\n${lines.join('\n')}`;
}

function formatValue(v) {
  if (v == null) return '_null_';
  if (typeof v === 'string') return v.length > 200 ? v.slice(0, 200) + '…' : v;
  if (typeof v === 'object') return '`' + JSON.stringify(v).slice(0, 200) + '`';
  return String(v);
}
