/**
 * Click-to-install platform catalog.
 *
 * The goal: a non-developer should be able to connect Plynf to whatever they
 * already use in ONE action — a copy, a config paste, or a marketplace
 * "Install" click. Each entry declares the single step for that platform.
 *
 * Used by the dashboard Setup page and the marketing /install page, so the
 * "how do I connect X?" answer is identical in-product and on the site.
 */

export type SetupKind =
  | "base-url" // SDKs: swap one line (copy)
  | "config" // desktop/IDE tools: paste a ready config (copy)
  | "marketplace" // automation platforms: one-click "Install" in their store
  | "cli"; // command-line

export interface Platform {
  id: string;
  name: string;
  tag: "AI SDK" | "Framework" | "Agent IDE" | "Automation" | "Local" | "Gateway";
  /** First letter monogram tint (on-brand, asset-free). */
  tint: "orange" | "magenta" | "indigo";
  blurb: string;
  kind: SetupKind;
  action: string; // primary button label
  href?: string; // marketplace / docs link
  /** Snippet to copy. ``__ENDPOINT__`` / ``__KEY__`` are replaced at render. */
  code?: string;
  lang?: string;
  steps: number; // number of user actions (1 = truly one-click-ish)
}

// The hosted endpoint + the per-tenant key placeholder. On the dashboard the
// Connect component swaps __KEY__ for the tenant's real key when available.
export const PLYNF_ENDPOINT = "https://app.plynf.com/v1";
export const PLYNF_KEY_PLACEHOLDER = "plynf_sk_live_…";

export const platforms: Platform[] = [
  {
    id: "openai-py",
    name: "OpenAI · Python",
    tag: "AI SDK",
    tint: "orange",
    blurb: "Swap the base URL. Every existing call is shaped + routed.",
    kind: "base-url",
    action: "Copy setup",
    lang: "python",
    steps: 1,
    code: `from openai import OpenAI

client = OpenAI(base_url="__ENDPOINT__", api_key="__KEY__")
# …your existing calls are unchanged.`,
  },
  {
    id: "openai-js",
    name: "OpenAI · Node",
    tag: "AI SDK",
    tint: "orange",
    blurb: "One line in your client config. Works with the official SDK.",
    kind: "base-url",
    action: "Copy setup",
    lang: "javascript",
    steps: 1,
    code: `import OpenAI from "openai";

const client = new OpenAI({ baseURL: "__ENDPOINT__", apiKey: "__KEY__" });`,
  },
  {
    id: "anthropic",
    name: "Anthropic",
    tag: "AI SDK",
    tint: "magenta",
    blurb: "Point the Anthropic SDK's base URL at Plynf's /v1/messages door.",
    kind: "base-url",
    action: "Copy setup",
    lang: "python",
    steps: 1,
    code: `from anthropic import Anthropic

client = Anthropic(base_url="https://app.plynf.com", api_key="__KEY__")`,
  },
  {
    id: "vercel-ai",
    name: "Vercel AI SDK",
    tag: "Framework",
    tint: "indigo",
    blurb: "Create an OpenAI provider pointed at Plynf, use it anywhere.",
    kind: "base-url",
    action: "Copy setup",
    lang: "javascript",
    steps: 1,
    code: `import { createOpenAI } from "@ai-sdk/openai";

export const plynf = createOpenAI({ baseURL: "__ENDPOINT__", apiKey: "__KEY__" });`,
  },
  {
    id: "langchain",
    name: "LangChain",
    tag: "Framework",
    tint: "indigo",
    blurb: "Set base_url on ChatOpenAI — chains, agents and tools all flow through.",
    kind: "base-url",
    action: "Copy setup",
    lang: "python",
    steps: 1,
    code: `from langchain_openai import ChatOpenAI

llm = ChatOpenAI(base_url="__ENDPOINT__", api_key="__KEY__", model="smart")`,
  },
  {
    id: "litellm",
    name: "LiteLLM",
    tag: "Gateway",
    tint: "orange",
    blurb: "Front your LiteLLM with Plynf — keep your routing, gain shaping.",
    kind: "base-url",
    action: "Copy setup",
    lang: "yaml",
    steps: 1,
    code: `model_list:
  - model_name: smart
    litellm_params:
      model: openai/smart
      api_base: __ENDPOINT__
      api_key: __KEY__`,
  },
  {
    id: "cursor",
    name: "Cursor",
    tag: "Agent IDE",
    tint: "magenta",
    blurb: "Set a custom OpenAI base URL in Settings → Models. Paste & go.",
    kind: "config",
    action: "Copy base URL",
    lang: "text",
    steps: 1,
    code: `Override OpenAI Base URL: __ENDPOINT__
API Key: __KEY__`,
  },
  {
    id: "claude-code",
    name: "Claude Code",
    tag: "Agent IDE",
    tint: "orange",
    blurb: "Export the base URL — Claude Code routes through Plynf's Anthropic door.",
    kind: "config",
    action: "Copy config",
    lang: "bash",
    steps: 1,
    code: `export ANTHROPIC_BASE_URL="https://app.plynf.com"
export ANTHROPIC_API_KEY="__KEY__"`,
  },
  {
    id: "continue",
    name: "Continue.dev",
    tag: "Agent IDE",
    tint: "indigo",
    blurb: "Drop this provider block into ~/.continue/config.json.",
    kind: "config",
    action: "Copy config",
    lang: "json",
    steps: 1,
    code: `{
  "models": [{
    "title": "Plynf",
    "provider": "openai",
    "model": "smart",
    "apiBase": "__ENDPOINT__",
    "apiKey": "__KEY__"
  }]
}`,
  },
  {
    id: "openwebui",
    name: "Open WebUI",
    tag: "Local",
    tint: "magenta",
    blurb: "Add an OpenAI-compatible connection in Admin → Settings → Connections.",
    kind: "config",
    action: "Copy connection",
    lang: "text",
    steps: 1,
    code: `API Base URL: __ENDPOINT__
API Key: __KEY__`,
  },
  {
    id: "ollama",
    name: "Ollama clients",
    tag: "Local",
    tint: "indigo",
    blurb: "Point any native-Ollama tool at Plynf's /api door — no rewrite.",
    kind: "config",
    action: "Copy host",
    lang: "bash",
    steps: 1,
    code: `export OLLAMA_HOST="https://app.plynf.com"`,
  },
  {
    id: "n8n",
    name: "n8n",
    tag: "Automation",
    tint: "orange",
    blurb: "Install the Plynf community node — connect with a key, no code.",
    kind: "marketplace",
    action: "Install in n8n",
    href: "https://www.npmjs.com/package/n8n-nodes-plynf",
    steps: 1,
  },
  {
    id: "zapier",
    name: "Zapier",
    tag: "Automation",
    tint: "magenta",
    blurb: "Add the Plynf app to a Zap. Authorize once, use in any step.",
    kind: "marketplace",
    action: "Add to Zapier",
    href: "https://zapier.com/apps/plynf/integrations",
    steps: 1,
  },
  {
    id: "make",
    name: "Make",
    tag: "Automation",
    tint: "indigo",
    blurb: "Install the Plynf module and drop it into any scenario.",
    kind: "marketplace",
    action: "Add to Make",
    href: "https://www.make.com/en/integrations/plynf",
    steps: 1,
  },
];

export const platformTags: Platform["tag"][] = [
  "AI SDK",
  "Framework",
  "Agent IDE",
  "Automation",
  "Local",
  "Gateway",
];

// One-click tool connectors (OAuth via the Plynf broker). The dashboard shows
// these as "Connect" buttons; Plynf shapes each tool's responses automatically.
export interface Connector {
  id: string;
  name: string;
  scopes: string;
  tint: "orange" | "magenta" | "indigo";
}

export const connectors: Connector[] = [
  { id: "github", name: "GitHub", scopes: "repo, read:user", tint: "orange" },
  { id: "slack", name: "Slack", scopes: "channels:read, chat:write", tint: "magenta" },
  { id: "google", name: "Google Workspace", scopes: "drive.readonly", tint: "indigo" },
  { id: "notion", name: "Notion", scopes: "read_content", tint: "orange" },
  { id: "linear", name: "Linear", scopes: "issues:read, issues:write", tint: "magenta" },
  { id: "salesforce", name: "Salesforce", scopes: "api, refresh_token", tint: "indigo" },
];
