/**
 * Click-to-install platform catalog.
 *
 * The goal: a non-developer should be able to connect Plynf to whatever they
 * already use in ONE action — a copy, a config paste, or a marketplace
 * "Install" click. Each entry declares the single step for that platform.
 *
 * Used by the dashboard Setup page and the marketing /install page, so the
 * "how do I connect X?" answer is identical in-product and on the site.
 *
 * Links are REAL: marketplace entries point at the integration source that
 * exists today (the repo's integrations/), relabelled when a public listing
 * goes live. SDK/IDE snippets are accurate working config.
 */

export type SetupKind =
  | "base-url" // SDKs: swap one line (copy)
  | "config" // desktop/IDE tools: paste a ready config (copy)
  | "marketplace" // automation platforms: install from a store / get the node
  | "cli"; // command-line

export interface Platform {
  id: string;
  name: string;
  tag: "AI SDK" | "Framework" | "Agent IDE" | "Automation" | "Local" | "Gateway";
  tint: "orange" | "magenta" | "indigo";
  blurb: string;
  kind: SetupKind;
  action: string; // primary button label
  href?: string; // marketplace / source link
  /** Snippet to copy. ``__ENDPOINT__`` / ``__KEY__`` are replaced at render. */
  code?: string;
  lang?: string;
  steps: number;
}

// The hosted endpoint + the per-tenant key placeholder. On the dashboard the
// Connect component swaps __KEY__ for the tenant's real key when available.
// Set PUBLIC_PLYNF_ENDPOINT / PUBLIC_PLYNF_BASE at build time to point at your
// own proxy origin (see GO_LIVE.md); defaults to app.plynf.com.
export const PLYNF_ENDPOINT = import.meta.env.PUBLIC_PLYNF_ENDPOINT || "https://app.plynf.com/v1";
export const PLYNF_BASE = import.meta.env.PUBLIC_PLYNF_BASE || "https://app.plynf.com"; // dialect doors
export const PLYNF_KEY_PLACEHOLDER = "plynf_sk_live_…";
const GH = "https://github.com/nico-schindlbeck-jpg/plynf/tree/main/integrations";

export const platforms: Platform[] = [
  // ── AI SDKs (base-url swap) ───────────────────────────────────────────────
  {
    id: "openai-py", name: "OpenAI · Python", tag: "AI SDK", tint: "orange", steps: 1,
    blurb: "Swap the base URL. Every existing call is shaped + routed.",
    kind: "base-url", action: "Copy setup", lang: "python",
    code: `from openai import OpenAI

client = OpenAI(base_url="__ENDPOINT__", api_key="__KEY__")
# …your existing calls are unchanged.`,
  },
  {
    id: "openai-js", name: "OpenAI · Node", tag: "AI SDK", tint: "orange", steps: 1,
    blurb: "One line in your client config. Works with the official SDK.",
    kind: "base-url", action: "Copy setup", lang: "javascript",
    code: `import OpenAI from "openai";

const client = new OpenAI({ baseURL: "__ENDPOINT__", apiKey: "__KEY__" });`,
  },
  {
    id: "anthropic-py", name: "Anthropic · Python", tag: "AI SDK", tint: "magenta", steps: 1,
    blurb: "Point the Anthropic SDK at Plynf's native /v1/messages door.",
    kind: "base-url", action: "Copy setup", lang: "python",
    code: `from anthropic import Anthropic

client = Anthropic(base_url="__BASE__", api_key="__KEY__")`,
  },
  {
    id: "anthropic-js", name: "Anthropic · TS", tag: "AI SDK", tint: "magenta", steps: 1,
    blurb: "baseURL on the Anthropic TS SDK — messages + token counting flow through.",
    kind: "base-url", action: "Copy setup", lang: "javascript",
    code: `import Anthropic from "@anthropic-ai/sdk";

const client = new Anthropic({ baseURL: "__BASE__", apiKey: "__KEY__" });`,
  },
  {
    id: "gemini", name: "Google Gemini", tag: "AI SDK", tint: "indigo", steps: 1,
    blurb: "Set the genai client's base URL — :generateContent routes via Plynf.",
    kind: "base-url", action: "Copy setup", lang: "python",
    code: `from google import genai

client = genai.Client(api_key="__KEY__",
    http_options={"base_url": "__BASE__"})`,
  },
  {
    id: "mistral", name: "Mistral", tag: "AI SDK", tint: "orange", steps: 1,
    blurb: "server_url on the Mistral SDK points at Plynf.",
    kind: "base-url", action: "Copy setup", lang: "python",
    code: `from mistralai import Mistral

client = Mistral(api_key="__KEY__", server_url="__ENDPOINT__")`,
  },
  {
    id: "cohere", name: "Cohere", tag: "AI SDK", tint: "magenta", steps: 1,
    blurb: "base_url on the Cohere v2 client — /v2/chat routes through Plynf.",
    kind: "base-url", action: "Copy setup", lang: "python",
    code: `import cohere

co = cohere.ClientV2(api_key="__KEY__", base_url="__BASE__")`,
  },
  {
    id: "azure-openai", name: "Azure OpenAI", tag: "AI SDK", tint: "indigo", steps: 1,
    blurb: "Point AzureOpenAI's endpoint at Plynf's Azure-shaped door.",
    kind: "base-url", action: "Copy setup", lang: "python",
    code: `from openai import AzureOpenAI

client = AzureOpenAI(azure_endpoint="__BASE__",
    api_key="__KEY__", api_version="2024-06-01")`,
  },
  {
    id: "vercel-ai", name: "Vercel AI SDK", tag: "AI SDK", tint: "orange", steps: 1,
    blurb: "Create an OpenAI provider pointed at Plynf, use it anywhere.",
    kind: "base-url", action: "Copy setup", lang: "javascript",
    code: `import { createOpenAI } from "@ai-sdk/openai";

export const plynf = createOpenAI({ baseURL: "__ENDPOINT__", apiKey: "__KEY__" });`,
  },
  // ── Frameworks ────────────────────────────────────────────────────────────
  {
    id: "langchain", name: "LangChain", tag: "Framework", tint: "indigo", steps: 1,
    blurb: "Set base_url on ChatOpenAI — chains, agents and tools all flow through.",
    kind: "base-url", action: "Copy setup", lang: "python",
    code: `from langchain_openai import ChatOpenAI

llm = ChatOpenAI(base_url="__ENDPOINT__", api_key="__KEY__", model="smart")`,
  },
  {
    id: "llamaindex", name: "LlamaIndex", tag: "Framework", tint: "magenta", steps: 1,
    blurb: "api_base on the OpenAI LLM — RAG + agents route via Plynf.",
    kind: "base-url", action: "Copy setup", lang: "python",
    code: `from llama_index.llms.openai import OpenAI

llm = OpenAI(api_base="__ENDPOINT__", api_key="__KEY__", model="smart")`,
  },
  {
    id: "pydantic-ai", name: "Pydantic AI", tag: "Framework", tint: "orange", steps: 1,
    blurb: "Point the OpenAI model's base_url at Plynf.",
    kind: "base-url", action: "Copy setup", lang: "python",
    code: `from pydantic_ai.models.openai import OpenAIModel

model = OpenAIModel("smart", base_url="__ENDPOINT__", api_key="__KEY__")`,
  },
  {
    id: "crewai", name: "CrewAI", tag: "Framework", tint: "indigo", steps: 1,
    blurb: "Set two env vars — every crew + agent routes through Plynf.",
    kind: "config", action: "Copy env", lang: "bash",
    code: `export OPENAI_API_BASE="__ENDPOINT__"
export OPENAI_API_KEY="__KEY__"`,
  },
  {
    id: "autogen", name: "AutoGen", tag: "Framework", tint: "magenta", steps: 1,
    blurb: "base_url in the model config — agents + group chats flow through.",
    kind: "config", action: "Copy config", lang: "python",
    code: `config_list = [{
    "model": "smart",
    "base_url": "__ENDPOINT__",
    "api_key": "__KEY__",
}]`,
  },
  {
    id: "haystack", name: "Haystack", tag: "Framework", tint: "indigo", steps: 1,
    blurb: "api_base_url on the OpenAI generator.",
    kind: "base-url", action: "Copy setup", lang: "python",
    code: `from haystack.components.generators import OpenAIGenerator

gen = OpenAIGenerator(api_base_url="__ENDPOINT__", model="smart")`,
  },
  {
    id: "litellm", name: "LiteLLM", tag: "Gateway", tint: "orange", steps: 1,
    blurb: "Front your LiteLLM with Plynf — keep your routing, gain shaping.",
    kind: "config", action: "Copy config", lang: "yaml",
    code: `model_list:
  - model_name: smart
    litellm_params:
      model: openai/smart
      api_base: __ENDPOINT__
      api_key: __KEY__`,
  },
  // ── Agent IDEs / chat UIs (config) ─────────────────────────────────────────
  {
    id: "cursor", name: "Cursor", tag: "Agent IDE", tint: "magenta", steps: 1,
    blurb: "Settings → Models → Override OpenAI Base URL. Paste these two.",
    kind: "config", action: "Copy base URL", lang: "text",
    code: `Override OpenAI Base URL: __ENDPOINT__
OpenAI API Key: __KEY__`,
  },
  {
    id: "claude-code", name: "Claude Code", tag: "Agent IDE", tint: "orange", steps: 1,
    blurb: "Export two vars — Claude Code routes through Plynf's Anthropic door.",
    kind: "config", action: "Copy config", lang: "bash",
    code: `export ANTHROPIC_BASE_URL="__BASE__"
export ANTHROPIC_API_KEY="__KEY__"`,
  },
  {
    id: "cline", name: "Cline", tag: "Agent IDE", tint: "indigo", steps: 1,
    blurb: "Pick \"OpenAI Compatible\", paste the base URL + key.",
    kind: "config", action: "Copy base URL", lang: "text",
    code: `Provider: OpenAI Compatible
Base URL: __ENDPOINT__
API Key:  __KEY__`,
  },
  {
    id: "aider", name: "Aider", tag: "Agent IDE", tint: "orange", steps: 1,
    blurb: "One flag (or env) and aider pairs through Plynf.",
    kind: "config", action: "Copy command", lang: "bash",
    code: `OPENAI_API_BASE=__ENDPOINT__ OPENAI_API_KEY=__KEY__ \\
  aider --model openai/smart`,
  },
  {
    id: "windsurf", name: "Windsurf", tag: "Agent IDE", tint: "magenta", steps: 1,
    blurb: "Add an OpenAI-compatible provider in settings, paste these.",
    kind: "config", action: "Copy base URL", lang: "text",
    code: `Base URL: __ENDPOINT__
API Key:  __KEY__`,
  },
  {
    id: "zed", name: "Zed", tag: "Agent IDE", tint: "indigo", steps: 1,
    blurb: "Set the OpenAI api_url in Zed's assistant settings.",
    kind: "config", action: "Copy settings", lang: "json",
    code: `"language_models": {
  "openai": { "api_url": "__ENDPOINT__" }
}`,
  },
  {
    id: "open-webui", name: "Open WebUI", tag: "Agent IDE", tint: "magenta", steps: 1,
    blurb: "Admin → Settings → Connections → add an OpenAI connection.",
    kind: "config", action: "Copy connection", lang: "text",
    code: `API Base URL: __ENDPOINT__
API Key:      __KEY__`,
  },
  {
    id: "librechat", name: "LibreChat", tag: "Agent IDE", tint: "orange", steps: 1,
    blurb: "Add a custom endpoint block to librechat.yaml.",
    kind: "config", action: "Copy config", lang: "yaml",
    code: `endpoints:
  custom:
    - name: "Plynf"
      baseURL: "__ENDPOINT__"
      apiKey: "__KEY__"
      models: { default: ["smart", "fast"] }`,
  },
  // ── Automation (real integration source) ───────────────────────────────────
  {
    id: "n8n", name: "n8n", tag: "Automation", tint: "orange", steps: 1,
    blurb: "Community node — connect with a key, drop into any workflow. No code.",
    kind: "marketplace", action: "Get the n8n node", href: `${GH}/n8n-nodes-plynf`,
  },
  {
    id: "zapier", name: "Zapier", tag: "Automation", tint: "magenta", steps: 1,
    blurb: "Plynf app for Zaps — authorize once, use in any step.",
    kind: "marketplace", action: "Get the Zapier app", href: `${GH}/zapier-plynf`,
  },
  {
    id: "make", name: "Make", tag: "Automation", tint: "indigo", steps: 1,
    blurb: "Plynf module — drop it into any scenario.",
    kind: "marketplace", action: "Get the Make module", href: `${GH}/make-plynf`,
  },
  {
    id: "copilot-studio", name: "Copilot Studio", tag: "Automation", tint: "magenta", steps: 1,
    blurb: "Microsoft Copilot Studio custom connector for Plynf.",
    kind: "marketplace", action: "Get the connector", href: `${GH}/copilot-studio-plynf`,
  },
  {
    id: "pipedream", name: "Pipedream", tag: "Automation", tint: "indigo", steps: 1,
    blurb: "In any code step, use the OpenAI SDK pointed at Plynf.",
    kind: "config", action: "Copy step", lang: "javascript",
    code: `import OpenAI from "openai";
const client = new OpenAI({ baseURL: "__ENDPOINT__", apiKey: "__KEY__" });`,
  },
  // ── Local ───────────────────────────────────────────────────────────────────
  {
    id: "ollama", name: "Ollama clients", tag: "Local", tint: "indigo", steps: 1,
    blurb: "Point any native-Ollama tool at Plynf's /api door — no rewrite.",
    kind: "config", action: "Copy host", lang: "bash",
    code: `export OLLAMA_HOST="__BASE__"`,
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
