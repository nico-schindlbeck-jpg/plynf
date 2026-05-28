// SPDX-License-Identifier: Apache-2.0
// Generic Plynf Create action: run a Plynf-managed tool and return the
// shaped response. One action covers every tool because the user picks
// the tool from a dropdown — keeps the Zapier app small and stops us
// having to re-publish every time we add a connector.

const TOOL_CHOICES = [
  { value: 'get_lead', label: 'Salesforce · Get Lead' },
  { value: 'list_leads', label: 'Salesforce · List Leads' },
  { value: 'get_account', label: 'Salesforce · Get Account' },
  { value: 'get_opportunity', label: 'Salesforce · Get Opportunity' },
  { value: 'get_contact', label: 'Salesforce · Get Contact' },
  { value: 'get_order', label: 'Order DB · Get Order' },
  { value: 'list_orders_by_customer', label: 'Order DB · List Orders By Customer' },
  { value: 'get_customer', label: 'Order DB · Get Customer' },
  { value: 'search_orders', label: 'Order DB · Search Orders' },
  { value: 'get_channel_messages', label: 'Slack · Get Channel Messages' },
  { value: 'search_messages', label: 'Slack · Search Messages' },
  { value: 'get_user_info', label: 'Slack · Get User Info' },
];

const perform = async (z, bundle) => {
  const { plynf_url } = bundle.authData;
  const { tool, arguments_json, agent_id } = bundle.inputData;

  let args;
  try {
    args = arguments_json ? JSON.parse(arguments_json) : {};
  } catch (e) {
    throw new z.errors.Error(
      `arguments must be valid JSON: ${e.message}`,
      'InvalidArguments',
      400,
    );
  }

  const response = await z.request({
    method: 'POST',
    url: `${plynf_url.replace(/\/+$/, '')}/v1/tools/${tool}/invoke`,
    body: { arguments: args, agent_id, workflow_id: bundle.meta?.zap?.id },
  });

  const body = response.data || {};
  return {
    tool,
    connector: body.connector,
    result: body.result,
    cache_hit: body.cache_hit,
    raw_response_tokens: body.savings?.raw_response_tokens,
    shaped_response_tokens: body.savings?.shaped_response_tokens,
    saved_tokens: body.savings?.saved_tokens,
    savings_pct: body.savings?.savings_pct,
  };
};

module.exports = {
  key: 'shape_tool_response',
  noun: 'Tool Response',
  display: {
    label: 'Fetch & shape a tool response with Plynf',
    description:
      'Calls a Plynf-managed tool (Salesforce, Slack, Order DB …) and returns the shaped JSON — typically 40–80% smaller than the raw response.',
  },
  operation: {
    perform,
    inputFields: [
      {
        key: 'tool',
        label: 'Tool',
        type: 'string',
        required: true,
        choices: TOOL_CHOICES,
        helpText: 'Which Plynf-managed tool to invoke.',
      },
      {
        key: 'arguments_json',
        label: 'Arguments (JSON)',
        type: 'text',
        required: false,
        default: '{}',
        helpText: 'Arguments for the tool. e.g. {"order_id":"12345"}',
      },
      {
        key: 'agent_id',
        label: 'Agent ID (optional)',
        type: 'string',
        required: false,
        helpText: 'Shown in your Plynf savings dashboard, useful for grouping.',
      },
    ],
    sample: {
      tool: 'get_order',
      connector: 'orders',
      result: {
        order_id: '12345',
        customer_name: 'Jane Doe',
        status: 'in_transit',
        tracking_number: 'DHL-99887766554433',
        estimated_delivery: '2026-05-28',
        carrier: 'DHL',
        items_summary: '3 items, $284.00 total',
      },
      cache_hit: false,
      raw_response_tokens: 2161,
      shaped_response_tokens: 83,
      saved_tokens: 2078,
      savings_pct: 0.9616,
    },
    outputFields: [
      { key: 'tool' },
      { key: 'connector' },
      { key: 'result', label: 'Shaped Result (JSON)' },
      { key: 'cache_hit', type: 'boolean' },
      { key: 'raw_response_tokens', type: 'integer' },
      { key: 'shaped_response_tokens', type: 'integer' },
      { key: 'saved_tokens', type: 'integer' },
      { key: 'savings_pct', type: 'number' },
    ],
  },
};
