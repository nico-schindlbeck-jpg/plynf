// SPDX-License-Identifier: Apache-2.0
// Zapier app entry point.
//
// Single action ("Fetch & shape a tool response") + custom auth. Auth headers
// are attached globally via `addAuthHeader` so individual actions don't
// repeat the Authorization wiring.

const authentication = require('./authentication');
const ShapeToolResponse = require('./creates/shape_tool_response');

const addAuthHeader = (request, z, bundle) => {
  if (bundle.authData?.api_key) {
    request.headers = request.headers || {};
    request.headers.Authorization = `Bearer ${bundle.authData.api_key}`;
    request.headers['Content-Type'] = 'application/json';
  }
  return request;
};

const handleErrors = (response, z) => {
  if (response.status === 402) {
    const body = response.data || {};
    throw new z.errors.Error(
      body.detail?.upgrade_hint || 'Plynf tier limit reached.',
      'TierLimitExceeded',
      402,
    );
  }
  return response;
};

module.exports = {
  version: require('./package.json').version,
  platformVersion: require('zapier-platform-core').version,
  authentication,
  beforeRequest: [addAuthHeader],
  afterResponse: [handleErrors],
  creates: {
    [ShapeToolResponse.key]: ShapeToolResponse,
  },
};
