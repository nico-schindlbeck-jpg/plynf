// SPDX-License-Identifier: Apache-2.0
// Plynf proxy client for the Slack bot.

const TOOLS = new Set([
  'get_lead',
  'list_leads',
  'get_account',
  'get_opportunity',
  'get_contact',
  'get_order',
  'list_orders_by_customer',
  'get_customer',
  'search_orders',
  'get_channel_messages',
  'search_messages',
  'get_user_info',
]);

export function isKnownTool(name) {
  return TOOLS.has(name);
}

export function listTools() {
  return [...TOOLS];
}

/**
 * Invoke a Plynf-managed tool via the webhook endpoint.
 * Returns { tool, connector, result, savings, cache_hit }.
 */
export async function shapeTool({
  baseUrl,
  apiKey,
  tool,
  args,
  agentId = 'slack-bot',
  workflowId,
}) {
  const url = `${baseUrl.replace(/\/+$/, '')}/v1/tools/${tool}/invoke`;
  const resp = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${apiKey}`,
    },
    body: JSON.stringify({
      arguments: args,
      agent_id: agentId,
      workflow_id: workflowId,
    }),
  });
  if (!resp.ok) {
    let detail = '';
    try {
      const body = await resp.json();
      detail = body?.detail?.upgrade_hint || body?.detail || JSON.stringify(body);
    } catch {
      detail = await resp.text();
    }
    throw new PlynfError(resp.status, String(detail).slice(0, 500));
  }
  return resp.json();
}

/**
 * Call the proxy's OpenAI-compatible /v1/chat/completions endpoint.
 * Used when the bot needs to *answer* a question using the shaped data
 * instead of just dumping the JSON into the channel.
 */
export async function chat({
  baseUrl,
  apiKey,
  model,
  systemPrompt,
  userPrompt,
  toolJson,
}) {
  const url = `${baseUrl.replace(/\/+$/, '')}/v1/chat/completions`;
  const messages = [];
  if (systemPrompt) messages.push({ role: 'system', content: systemPrompt });
  messages.push({
    role: 'user',
    content:
      `${userPrompt}\n\n` +
      (toolJson ? `Shaped tool response:\n\`\`\`\n${JSON.stringify(toolJson, null, 2)}\n\`\`\`` : ''),
  });
  const resp = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${apiKey}`,
    },
    body: JSON.stringify({ model, messages }),
  });
  if (!resp.ok) throw new PlynfError(resp.status, await resp.text());
  const body = await resp.json();
  return body.choices?.[0]?.message?.content ?? '';
}

export class PlynfError extends Error {
  constructor(status, detail) {
    super(`Plynf ${status}: ${detail}`);
    this.status = status;
    this.detail = detail;
  }
}
