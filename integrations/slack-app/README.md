# Plynf Slack Bot

A deployable Slack bot that exposes Plynf-managed tools to your Slack
workspace as both `@plynf` mentions and the `/plynf-fetch` slash command.

## ⚡ Fastest path to a running bot

| Method | Time | Command / Link |
|---|---|---|
| **OAuth ("Add to Slack")** | 30 s | Visit `https://app.plynf.com/slack/install`, click Allow, done |
| **Render** | 2 min | [Deploy to Render](https://render.com/deploy?repo=https://github.com/nico-schindlbeck-jpg/plynf) — fills env vars during onboarding |
| **Fly.io** | 3 min | `cd integrations/slack-app && flyctl launch --copy-config` |
| **Railway** | 2 min | [Deploy on Railway](https://railway.app/new/template?template=https://github.com/nico-schindlbeck-jpg/plynf) |
| **Docker** | 5 min | `docker run --env-file .env -p 3000:3000 ghcr.io/nico-schindlbeck-jpg/plynf-slack-bot:latest` |
| **From source** | 10 min | Scroll to *"Manual install"* below |

## What you get

- **Mention support** — Users write `@plynf what is the status of order #12345?`
  in any channel. The bot extracts the order id, calls Plynf, posts a
  shaped answer in the thread. Optional: pipe the shaped JSON through an
  LLM (Plynf-proxy speaks OpenAI) for a friendly natural-language reply.
- **Slash command** — `/plynf-fetch get_order {"order_id":"12345"}` for
  power users. Reply is ephemeral, includes the savings block so the
  caller sees the token reduction inline.
- **App home tab** — onboarding card the first time a user opens the bot's
  profile.

## Manual install

### Step 1 — Create the Slack app

1. https://api.slack.com/apps → **Create New App** → **From a manifest**
2. Paste the contents of `manifest.yaml` (sibling file in this directory)
3. Pick the workspace, hit **Create**
4. **Install to Workspace**
5. From **OAuth & Permissions**: copy the **Bot User OAuth Token** (`xoxb-…`)
6. From **Basic Information**: copy the **Signing Secret**
7. (Dev only) From **Socket Mode**: generate an **App-Level Token** with
   `connections:write` scope (`xapp-…`). Production deploys leave this
   blank and run in HTTP mode.

### Step 2 — Configure the worker

```sh
cd integrations/slack-app
cp .env.example .env
# Edit .env: paste the three Slack tokens, point PLYNF_URL at your proxy
```

### Step 3 — Run it

```sh
npm install
npm start
```

You should see:

```
⚡ Plynf Slack bot up (Socket Mode) · proxy=http://localhost:7430
```

Now in Slack:

- `@plynf hello` — shows the help text
- `@plynf what is the status of order #12345?` — fetches a shaped order
  via the Plynf proxy and replies in-thread
- `/plynf-fetch get_order {"order_id":"12345"}` — slash-command variant

### Step 4 — Production (HTTP mode)

Drop the `SLACK_APP_TOKEN` env var and deploy behind a public URL. The
bot listens on `$PORT` (default 3000). Update the Slack app's
**Event Subscriptions** URL to `https://your-domain/slack/events`.

Container is provided:

```sh
docker build -t plynf-slack-bot .
docker run --env-file .env -p 3000:3000 plynf-slack-bot
```

## Tier gating

Same as every Plynf surface: the proxy enforces tier limits, the bot
forwards the 402 + `upgrade_hint` body verbatim. Free-tier users hitting
the monthly cap see a clear in-thread "Plynf tier limit reached: upgrade
to Pro for 5M tokens" message — no special handling needed in the bot.

## Layout

```
slack-app/
├── manifest.yaml          # Slack app configuration (paste into Slack UI)
├── package.json           # @slack/bolt + dotenv
├── Dockerfile             # production container
├── docker/
│   └── docker-compose.yml # local-dev compose
├── src/
│   ├── app.js             # entry point — wires bolt + handlers
│   ├── plynf-client.js    # /v1/tools/{tool}/invoke + /v1/chat/completions
│   └── handlers/
│       ├── mention.js     # @plynf event handler
│       └── slash.js       # /plynf-fetch command handler
└── .env.example
```

## License

Apache-2.0
