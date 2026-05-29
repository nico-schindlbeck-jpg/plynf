// SPDX-License-Identifier: Apache-2.0
// One-shot script: register the bot's slash commands with Discord.
// Run via `npm run register-commands` after editing TOOLS or args.

import 'dotenv/config';
import { REST, Routes, SlashCommandBuilder } from 'discord.js';
import { listTools } from './plynf-client.js';

const cmd = new SlashCommandBuilder()
  .setName('plynf')
  .setDescription('Fetch & shape a tool response via Plynf.')
  .addStringOption((opt) =>
    opt
      .setName('tool')
      .setDescription('Which Plynf-managed tool to invoke')
      .setRequired(true)
      .addChoices(...listTools().map((t) => ({ name: t, value: t }))),
  )
  .addStringOption((opt) =>
    opt
      .setName('args')
      .setDescription('JSON-encoded arguments, e.g. {"order_id":"12345"}')
      .setRequired(false),
  );

const token = process.env.DISCORD_TOKEN;
const clientId = process.env.DISCORD_CLIENT_ID;
const guildId = process.env.DISCORD_GUILD_ID || null;

if (!token || !clientId) {
  console.error('DISCORD_TOKEN and DISCORD_CLIENT_ID are required.');
  process.exit(1);
}

const rest = new REST({ version: '10' }).setToken(token);
const route = guildId
  ? Routes.applicationGuildCommands(clientId, guildId)
  : Routes.applicationCommands(clientId);

const body = [cmd.toJSON()];
await rest.put(route, { body });
console.log(
  guildId
    ? `Registered /plynf in guild ${guildId} (visible immediately).`
    : `Registered /plynf globally (up to 1h to propagate).`,
);
