# Plynf Discord Bot

A small Discord bot exposing Plynf-managed tools via a single
`/plynf <tool> <args>` slash command. Designed for developer / community
Discord servers where you want to demo Plynf without standing up a
whole agent.

## Deploy

### Step 1 — Create the Discord application

1. https://discord.com/developers/applications → **New Application**
2. Copy the **Application ID** (= `DISCORD_CLIENT_ID`)
3. **Bot** tab → **Reset Token** → copy (= `DISCORD_TOKEN`)
4. Under **OAuth2 → URL Generator** check scopes `bot` and
   `applications.commands`. Paste the generated URL into a browser to
   invite the bot to your test server. Note your server's **guild id**
   (right-click server icon → *Copy ID* with developer mode on).

### Step 2 — Configure + register the command

```sh
cd integrations/discord-bot
cp .env.example .env
# Edit .env with tokens + a guild id for instant registration
npm install
npm run register-commands
```

You should see:

```
Registered /plynf in guild 123456789012345678 (visible immediately).
```

### Step 3 — Run

```sh
npm start
```

In Discord, type `/plynf` in any channel — autocomplete shows the tool
list. Pick one, optionally pass `args` as JSON. The bot replies
ephemerally (only you see it) with the shaped result + savings header.

## Tier gating

Same as everywhere — the proxy returns 402 with `upgrade_hint` when
the Free-tier cap is hit; the bot surfaces the message.

## License

Apache-2.0
