// SPDX-License-Identifier: Apache-2.0
// Plynf Discord bot — gateway-based interaction handler.

import 'dotenv/config';
import { Client, Events, GatewayIntentBits } from 'discord.js';
import { shapeTool, isKnownTool, listTools, PlynfError } from './plynf-client.js';

const cfg = {
  token: req('DISCORD_TOKEN'),
  plynfUrl: req('PLYNF_URL'),
  plynfApiKey: req('PLYNF_API_KEY'),
};

function req(name) {
  const v = process.env[name];
  if (!v) { console.error(`Missing env var: ${name}`); process.exit(1); }
  return v;
}

const client = new Client({ intents: [GatewayIntentBits.Guilds] });

client.once(Events.ClientReady, (c) => {
  console.log(`⚡ Plynf Discord bot ready as ${c.user.tag}`);
});

client.on(Events.InteractionCreate, async (interaction) => {
  if (!interaction.isChatInputCommand()) return;
  if (interaction.commandName !== 'plynf') return;

  await interaction.deferReply({ ephemeral: true });

  const tool = interaction.options.getString('tool', true);
  const argsRaw = interaction.options.getString('args') ?? '{}';
  if (!isKnownTool(tool)) {
    await interaction.editReply(
      `Unknown tool \`${tool}\`. Known: ${listTools().join(', ')}`,
    );
    return;
  }
  let args;
  try { args = JSON.parse(argsRaw); }
  catch (e) {
    await interaction.editReply(`Could not parse JSON args: ${e.message}`);
    return;
  }

  try {
    const shaped = await shapeTool({
      baseUrl: cfg.plynfUrl,
      apiKey: cfg.plynfApiKey,
      tool,
      args,
      agentId: `discord:${interaction.user.id}`,
      workflowId: `discord:${interaction.channelId}`,
    });
    await interaction.editReply(renderEmbed(tool, shaped));
  } catch (err) {
    if (err instanceof PlynfError && err.status === 402) {
      await interaction.editReply(`⚠️ Plynf tier limit reached. ${err.detail}`);
    } else {
      await interaction.editReply(`❌ Plynf request failed: ${err.message}`);
    }
  }
});

function renderEmbed(tool, shaped) {
  const r = shaped.result ?? {};
  const savings = shaped.savings ?? {};
  const pct =
    typeof savings.savings_pct === 'number'
      ? (savings.savings_pct * 100).toFixed(0) + '%'
      : '?';
  const head = `**${tool}** · saved ${pct} tokens` +
    (shaped.cache_hit ? ' · ⚡ cache hit' : '');
  const json = JSON.stringify(r, null, 2);
  // Discord's message cap is ~2000 chars. Trim defensively.
  return `${head}\n\`\`\`json\n${json.slice(0, 1800)}\n\`\`\``;
}

await client.login(cfg.token);
