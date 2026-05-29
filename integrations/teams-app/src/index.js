// SPDX-License-Identifier: Apache-2.0
// Plynf Teams bot — restify entry point.

import 'dotenv/config';
import restify from 'restify';
import {
  CloudAdapter,
  ConfigurationServiceClientCredentialFactory,
  ConfigurationBotFrameworkAuthentication,
} from 'botbuilder';
import { PlynfBot } from './bot.js';

const cfg = {
  microsoftAppId: req('MICROSOFT_APP_ID'),
  microsoftAppPassword: req('MICROSOFT_APP_PASSWORD'),
  microsoftAppType: process.env.MICROSOFT_APP_TYPE || 'MultiTenant',
  microsoftAppTenantId: process.env.MICROSOFT_APP_TENANT_ID || '',
  plynfUrl: req('PLYNF_URL'),
  plynfApiKey: req('PLYNF_API_KEY'),
  plynfModel: process.env.PLYNF_MODEL || '',
  port: parseInt(process.env.PORT || '3978', 10),
};

function req(name) {
  const v = process.env[name];
  if (!v) { console.error(`Missing required env var: ${name}`); process.exit(1); }
  return v;
}

const credentialsFactory = new ConfigurationServiceClientCredentialFactory({
  MicrosoftAppId: cfg.microsoftAppId,
  MicrosoftAppPassword: cfg.microsoftAppPassword,
  MicrosoftAppType: cfg.microsoftAppType,
  MicrosoftAppTenantId: cfg.microsoftAppTenantId,
});
const botFrameworkAuthentication = new ConfigurationBotFrameworkAuthentication(
  {},
  credentialsFactory,
);
const adapter = new CloudAdapter(botFrameworkAuthentication);
adapter.onTurnError = async (context, error) => {
  console.error('Bot turn error:', error);
  await context.sendActivity('Sorry — the bot hit an error processing that message.');
};

const bot = new PlynfBot(cfg);

const server = restify.createServer();
server.use(restify.plugins.bodyParser());

server.post('/api/messages', async (req, res) => {
  await adapter.process(req, res, (ctx) => bot.run(ctx));
});

server.listen(cfg.port, () => {
  console.log(`⚡ Plynf Teams bot up on :${cfg.port} · proxy=${cfg.plynfUrl}`);
});
