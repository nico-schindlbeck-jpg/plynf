// SPDX-License-Identifier: Apache-2.0
// Plynf node for n8n — one drag-and-drop step that fetches and shapes a
// CRM/ERP/Slack/etc. response before sending it to your LLM step.

import type {
  IExecuteFunctions,
  INodeExecutionData,
  INodeType,
  INodeTypeDescription,
  IDataObject,
} from 'n8n-workflow';

import { NodeOperationError } from 'n8n-workflow';

// Tool names recognised by Plynf. Kept in sync with TOOL_TO_CONNECTOR
// in services/proxy/src/plinth_proxy/connectors.py.
const TOOL_OPTIONS = [
  { name: 'Salesforce · Get Lead', value: 'get_lead' },
  { name: 'Salesforce · List Leads', value: 'list_leads' },
  { name: 'Salesforce · Get Opportunity', value: 'get_opportunity' },
  { name: 'Salesforce · Get Account', value: 'get_account' },
  { name: 'Salesforce · Get Contact', value: 'get_contact' },
  { name: 'Order DB · Get Order', value: 'get_order' },
  { name: 'Order DB · List Orders By Customer', value: 'list_orders_by_customer' },
  { name: 'Order DB · Get Customer', value: 'get_customer' },
  { name: 'Order DB · Search Orders', value: 'search_orders' },
  { name: 'Slack · Get Channel Messages', value: 'get_channel_messages' },
  { name: 'Slack · Search Messages', value: 'search_messages' },
  { name: 'Slack · Get User Info', value: 'get_user_info' },
];

export class Plynf implements INodeType {
  description: INodeTypeDescription = {
    displayName: 'Plynf',
    name: 'plynf',
    icon: 'file:plynf.svg',
    group: ['transform'],
    version: 1,
    subtitle: '={{$parameter["tool"]}}',
    description:
      'Fetch a tool response (Salesforce, Slack, Order DB, …) through Plynf, which trims it to the fields your agent actually needs.',
    defaults: { name: 'Plynf' },
    inputs: ['main'],
    outputs: ['main'],
    credentials: [{ name: 'plynfApi', required: true }],
    properties: [
      {
        displayName: 'Tool',
        name: 'tool',
        type: 'options',
        options: TOOL_OPTIONS,
        default: 'get_order',
        description: 'Which Plynf-managed tool to invoke',
        required: true,
      },
      {
        displayName: 'Arguments (JSON)',
        name: 'argumentsJson',
        type: 'json',
        default: '{}',
        description:
          'Arguments passed to the tool. For example: {"order_id": "12345"}',
      },
      {
        displayName: 'Agent ID',
        name: 'agentId',
        type: 'string',
        default: '',
        description:
          'Optional agent identifier shown in your Plynf savings dashboard',
      },
      {
        displayName: 'Workflow ID',
        name: 'workflowId',
        type: 'string',
        default: '={{$workflow.id}}',
        description:
          'Defaults to this n8n workflow id so savings can be grouped per workflow',
      },
    ],
  };

  async execute(this: IExecuteFunctions): Promise<INodeExecutionData[][]> {
    const creds = (await this.getCredentials('plynfApi')) as {
      plynfUrl: string;
      apiKey: string;
    };

    const items = this.getInputData();
    const out: INodeExecutionData[] = [];

    for (let i = 0; i < items.length; i++) {
      const tool = this.getNodeParameter('tool', i) as string;
      const argsRaw = this.getNodeParameter('argumentsJson', i, '{}');
      const agentId = this.getNodeParameter('agentId', i, '') as string;
      const workflowId = this.getNodeParameter('workflowId', i, '') as string;

      let args: IDataObject;
      if (typeof argsRaw === 'string') {
        try {
          args = JSON.parse(argsRaw);
        } catch (e) {
          throw new NodeOperationError(
            this.getNode(),
            `Arguments JSON is not valid JSON: ${(e as Error).message}`,
            { itemIndex: i },
          );
        }
      } else {
        args = (argsRaw ?? {}) as IDataObject;
      }

      const url = `${creds.plynfUrl.replace(/\/+$/, '')}/v1/tools/${tool}/invoke`;
      const body: IDataObject = { arguments: args };
      if (agentId) body.agent_id = agentId;
      if (workflowId) body.workflow_id = workflowId;

      const response = await this.helpers.httpRequest({
        method: 'POST',
        url,
        body,
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${creds.apiKey}`,
        },
        json: true,
      });

      out.push({
        json: {
          tool,
          result: (response as IDataObject).result,
          cache_hit: (response as IDataObject).cache_hit,
          savings: (response as IDataObject).savings,
        },
        pairedItem: { item: i },
      });
    }

    return [out];
  }
}
