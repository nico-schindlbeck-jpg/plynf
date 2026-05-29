# Plynf Microsoft Teams Bot

Deployable Teams bot built on the Microsoft Bot Framework SDK.
Users can `@Plynf` in a channel or chat with the bot directly; the bot
fetches Plynf-managed business data and shapes it before replying.

## ⚡ Fastest path to a running bot

| Method | Time | Command / Link |
|---|---|---|
| **Render** | 3 min | [Deploy to Render](https://render.com/deploy?repo=https://github.com/nico-schindlbeck-jpg/plynf) — fills env vars during onboarding |
| **Fly.io** | 4 min | `cd integrations/teams-app && flyctl launch --copy-config` |
| **Docker** | 5 min | `docker run --env-file .env -p 3978:3978 ghcr.io/nico-schindlbeck-jpg/plynf-teams-bot:latest` |
| **From source** | 15 min | Scroll to *"Manual install"* below |

After the worker runs, register the bot in [Azure Bot Framework](https://dev.botframework.com) and upload `manifest/manifest.json` to Teams.

## What you get

- **`@Plynf what is the status of order #12345?`** — natural-language
  trigger. The bot keyword-matches the question to a Plynf tool, fetches
  shaped data, replies in the channel.
- **`fetch get_order {"order_id":"12345"}`** — direct power-user command.
- **`help`** — shows what tools the bot knows.
- Optional LLM-drafted reply when `PLYNF_MODEL` is set, otherwise a
  compact Markdown summary of the shaped JSON.

## Manual install

### Step 1 — Register the bot in Azure / Bot Framework

1. https://dev.botframework.com → **My bots** → **Create**
   (or use the Bot Framework Composer / Azure portal Bot Service blade)
2. Pick **Multi-tenant**, get the **App ID + password** (client secret)
3. Set the **Messaging endpoint** to `https://<your-domain>/api/messages`
4. Enable the **Microsoft Teams** channel

### Step 2 — Package the Teams app manifest

1. Edit `manifest/manifest.json`:
   - Generate a new GUID for `id`
   - Paste the Azure App ID into `bots[0].botId`
2. Add 192×192 PNG (`color.png`) and 32×32 transparent PNG (`outline.png`)
   icons next to `manifest.json`
3. Zip the three files into `plynf-teams.zip`

### Step 3 — Run the worker

```sh
cd integrations/teams-app
cp .env.example .env
# Fill in Microsoft app credentials + Plynf URL
npm install
npm start
```

Or as a container:

```sh
docker build -t plynf-teams-bot .
docker run --env-file .env -p 3978:3978 plynf-teams-bot
```

### Step 4 — Install in Teams

In Teams admin: **Apps → Manage your apps → Upload an app** → upload
`plynf-teams.zip`. Add to a team or chat to start using it.

## Tier gating

Same as every Plynf surface — the proxy returns HTTP 402 with an
`upgrade_hint` when the tenant hits the Free-tier cap, and the bot
forwards that message to the user.

## License

Apache-2.0
