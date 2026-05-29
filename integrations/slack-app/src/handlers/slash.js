// SPDX-License-Identifier: Apache-2.0
// /plynf-fetch slash-command handler.
//
// Usage:
//   /plynf-fetch get_order {"order_id":"12345"}
//   /plynf-fetch list_leads
//
// Behaviour:
//   1. Parse the command into <tool> <jsonArgs>
//   2. Invoke Plynf, fetch the shaped result
//   3. Reply in the channel as an ephemeral message (only the caller sees it)
//   4. Surface savings inline so the user *sees* the reduction.

import { shapeTool, isKnownTool, listTools, PlynfError } from '../plynf-client.js';

export function registerSlash(app, cfg) {
  app.command('/plynf-fetch', async ({ command, ack, respond }) => {
    await ack();

    const text = (command.text || '').trim();
    if (!text) {
      await respond({
        response_type: 'ephemeral',
        text:
          'Usage: `/plynf-fetch <tool> {json args}`\n' +
          `Known tools: ${listTools().join(', ')}`,
      });
      return;
    }

    // Split into "<tool>" and the rest as JSON.
    const firstSpace = text.indexOf(' ');
    const tool = firstSpace === -1 ? text : text.slice(0, firstSpace).trim();
    const argsRaw = firstSpace === -1 ? '{}' : text.slice(firstSpace + 1).trim();

    if (!isKnownTool(tool)) {
      await respond({
        response_type: 'ephemeral',
        text: `:warning: Unknown tool \`${tool}\`. Available: ${listTools().join(', ')}`,
      });
      return;
    }

    let args;
    try {
      args = argsRaw ? JSON.parse(argsRaw) : {};
    } catch (e) {
      await respond({
        response_type: 'ephemeral',
        text: `:warning: Could not parse JSON arguments: ${e.message}`,
      });
      return;
    }

    try {
      const result = await shapeTool({
        baseUrl: cfg.plynfUrl,
        apiKey: cfg.plynfApiKey,
        tool,
        args,
        agentId: `slack:${command.user_id}`,
        workflowId: `slash:${command.channel_id}`,
      });
      await respond({
        response_type: 'ephemeral',
        blocks: renderToolBlocks(tool, result),
      });
    } catch (err) {
      if (err instanceof PlynfError && err.status === 402) {
        await respond({
          response_type: 'ephemeral',
          text: `:warning: Plynf tier limit reached. ${err.detail}`,
        });
      } else {
        await respond({
          response_type: 'ephemeral',
          text: `:x: Plynf request failed: ${err.message}`,
        });
      }
    }
  });
}

function renderToolBlocks(tool, result) {
  const savings = result.savings ?? {};
  const pct =
    typeof savings.savings_pct === 'number'
      ? (savings.savings_pct * 100).toFixed(1)
      : null;
  const summary =
    pct !== null
      ? `*${tool}* · saved *${pct}%* tokens (` +
        `${savings.raw_response_tokens?.toLocaleString?.() ?? '?'} → ` +
        `${savings.shaped_response_tokens?.toLocaleString?.() ?? '?'})` +
        (result.cache_hit ? ' · :zap: cache hit' : '')
      : `*${tool}* response`;
  const json = JSON.stringify(result.result ?? {}, null, 2);
  return [
    { type: 'section', text: { type: 'mrkdwn', text: summary } },
    {
      type: 'section',
      text: { type: 'mrkdwn', text: '```\n' + json.slice(0, 2900) + '\n```' },
    },
  ];
}
