// SPDX-License-Identifier: Apache-2.0
// Plynf authentication for Zapier.
//
// Custom auth: the user pastes their Plynf URL (default app.plynf.com)
// and their API key. We verify by calling GET /v1/tier, which echoes the
// resolved tenant + tier — that's the same proof of credentials our
// dashboard uses.

const test = async (z, bundle) => {
  const response = await z.request({
    url: `${bundle.authData.plynf_url.replace(/\/+$/, '')}/v1/tier`,
    method: 'GET',
  });
  // Zapier auto-throws on >= 400 since v15.
  return response.data;
};

module.exports = {
  type: 'custom',
  fields: [
    {
      key: 'plynf_url',
      label: 'Plynf URL',
      type: 'string',
      required: true,
      default: 'https://app.plynf.com',
      helpText:
        'The base URL of your Plynf proxy. Leave the default unless you are self-hosting.',
    },
    {
      key: 'api_key',
      label: 'API Key',
      type: 'string',
      required: true,
      helpText:
        'Get your API key at https://app.plynf.com — free tier available. Free includes 100k shaped tokens / month and 3 connectors.',
    },
  ],
  test,
  connectionLabel: '{{tenant_id}} ({{tier}})',
};
