// SPDX-License-Identifier: Apache-2.0
// Plynf Slack bot — entry point.
//
// Two run modes:
//   - Socket Mode (default in dev): no public URL needed; bot keeps a
//     persistent WebSocket open with Slack. Activated when SLACK_APP_TOKEN
//     is present.
//   - HTTP Mode (default in prod): bot exposes an Express-style HTTP server
//     on $PORT (3000). Slack POSTs events to /slack/events.

import 'dotenv/config';
import bolt from '@slack/bolt';
import { registerMention } from './handlers/mention.js';
import { registerSlash } from './handlers/slash.js';

const { App, LogLevel } = bolt;

const cfg = {
  slackBotToken: required('SLACK_BOT_TOKEN'),
  slackSigningSecret: required('SLACK_SIGNING_SECRET'),
  slackAppToken: process.env.SLACK_APP_TOKEN || '',
  plynfUrl: required('PLYNF_URL'),
  plynfApiKey: required('PLYNF_API_KEY'),
  plynfModel: process.env.PLYNF_MODEL || '',
  port: parseInt(process.env.PORT || '3000', 10),
  logLevel: (process.env.LOG_LEVEL || 'info').toLowerCase(),
};

function required(name) {
  const v = process.env[name];
  if (!v) {
    console.error(`Missing required env var: ${name}`);
    process.exit(1);
  }
  return v;
}

const socketMode = Boolean(cfg.slackAppToken);

const app = new App({
  token: cfg.slackBotToken,
  signingSecret: cfg.slackSigningSecret,
  socketMode,
  appToken: socketMode ? cfg.slackAppToken : undefined,
  logLevel: LogLevel[cfg.logLevel?.toUpperCase()] ?? LogLevel.INFO,
});

// Wire handlers
registerMention(app, cfg);
registerSlash(app, cfg);

// Friendly home tab the first time a user opens the bot's profile.
app.event('app_home_opened', async ({ event, client }) => {
  await client.views.publish({
    user_id: event.user,
    view: {
      type: 'home',
      blocks: [
        {
          type: 'header',
          text: { type: 'plain_text', text: 'Plynf — Agent Context Optimization' },
        },
        {
          type: 'section',
          text: {
            type: 'mrkdwn',
            text:
              `Hi <@${event.user}>! I fetch business-system data (Salesforce, Order DB, Slack, …) and ` +
              `shape it down to the fields your AI agents *actually* use — saving 40–80% of tokens per call.`,
          },
        },
        {
          type: 'section',
          text: {
            type: 'mrkdwn',
            text:
              '*How to use*\n' +
              '• Mention me in any channel: `@plynf what is the status of order #12345?`\n' +
              '• Slash command for direct access: `/plynf-fetch get_order {"order_id":"12345"}`\n' +
              '• Read the docs at https://plynf.com/docs/slack',
          },
        },
      ],
    },
  });
});

(async () => {
  if (socketMode) {
    await app.start();
    console.log(`⚡ Plynf Slack bot up (Socket Mode) · proxy=${cfg.plynfUrl}`);
  } else {
    await app.start(cfg.port);
    console.log(
      `⚡ Plynf Slack bot up on :${cfg.port} (HTTP Mode) · proxy=${cfg.plynfUrl}`,
    );
  }
})().catch((err) => {
  console.error('failed to start:', err);
  process.exit(1);
});
