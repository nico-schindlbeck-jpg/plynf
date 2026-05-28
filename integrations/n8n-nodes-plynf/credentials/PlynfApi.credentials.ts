// SPDX-License-Identifier: Apache-2.0
// Plynf API credentials for n8n.

import type {
  ICredentialType,
  INodeProperties,
} from 'n8n-workflow';

export class PlynfApi implements ICredentialType {
  name = 'plynfApi';
  displayName = 'Plynf API';
  documentationUrl = 'https://plynf.com/docs/integrations/n8n';

  properties: INodeProperties[] = [
    {
      displayName: 'Plynf URL',
      name: 'plynfUrl',
      type: 'string',
      default: 'https://app.plynf.com',
      description: 'Base URL of your Plynf proxy. Use http://localhost:7430 for local testing.',
      required: true,
    },
    {
      displayName: 'API Key',
      name: 'apiKey',
      type: 'string',
      typeOptions: { password: true },
      default: '',
      description: 'Your Plynf API key. Free tier keys are issued at https://app.plynf.com.',
      required: true,
    },
  ];
}
