// SPDX-License-Identifier: Apache-2.0
// Shared Plynf client for the Discord bot.

const TOOLS = [
  'get_lead', 'list_leads', 'get_account', 'get_opportunity', 'get_contact',
  'get_order', 'list_orders_by_customer', 'get_customer', 'search_orders',
  'get_channel_messages', 'search_messages', 'get_user_info',
];

export function listTools() { return TOOLS; }
export function isKnownTool(name) { return TOOLS.includes(name); }

export async function shapeTool({ baseUrl, apiKey, tool, args, agentId, workflowId }) {
  const url = `${baseUrl.replace(/\/+$/, '')}/v1/tools/${tool}/invoke`;
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${apiKey}` },
    body: JSON.stringify({ arguments: args, agent_id: agentId, workflow_id: workflowId }),
  });
  if (!resp.ok) {
    let detail = '';
    try { const body = await resp.json(); detail = body?.detail?.upgrade_hint || body?.detail || JSON.stringify(body); }
    catch { detail = await resp.text(); }
    throw new PlynfError(resp.status, String(detail).slice(0, 500));
  }
  return resp.json();
}

export class PlynfError extends Error {
  constructor(status, detail) { super(`Plynf ${status}: ${detail}`); this.status = status; this.detail = detail; }
}
