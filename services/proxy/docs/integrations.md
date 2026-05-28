# Plynf — Integration Recipes

Plynf's `/v1/chat/completions` endpoint speaks OpenAI's wire format. That
means any client / framework / runtime that supports a configurable
`base_url` works out of the box, with no Plynf-specific code. This page
collects the one-liners that get each provider running.

---

## 1. OpenAI (the easy case)

```python
from openai import OpenAI

client = OpenAI(
    api_key="sk-...",
    base_url="https://app.plynf.com/v1",
)

resp = client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "What's the status of order 12345?"}],
    tools=[{"type": "function", "function": {"name": "get_order"}}],
)
```

The same works with the official Node SDK (`openai` on npm) and any
third-party client that follows the OpenAI spec.

---

## 2. Azure OpenAI

Azure's deployment-based URL means you point Plynf at the upstream, not
the other way around.

Run Plynf with the Azure endpoint as upstream:

```bash
export PLINTH_PROXY_DEMO_MODE=false
export PLINTH_PROXY_UPSTREAM_BASE_URL="https://<resource>.openai.azure.com/openai/deployments/<deployment>"
export PLINTH_PROXY_UPSTREAM_API_KEY="<your-azure-key>"
plinth-proxy
```

Your client speaks vanilla OpenAI to Plynf; Plynf translates to Azure on
the upstream side.

> Limitation: Azure has its own request-shape quirks (e.g. `api-version`
> in query string). The MVP forwards transparently, so include
> `api-version=2024-10-21` in your `base_url` if the upstream requires it.

---

## 3. Anthropic (Claude)

Two ways:

**a) MCP-native** — pass Plynf as an MCP server in your Claude API client:

```python
client.messages.create(
    model="claude-3-5-sonnet-20241022",
    mcp_servers=[{"url": "https://app.plynf.com/mcp/orders"}],
    ...
)
```

**b) Anthropic-shaped proxy** — once `/v1/messages` ships (Sprint 2 P6),
point any Anthropic SDK at Plynf the same way as OpenAI.

---

## 4. Ollama (local models)

Ollama's `/v1/chat/completions` is OpenAI-compatible, so the proxy chains
cleanly: point your client at Plynf, point Plynf at Ollama.

```bash
# Start Ollama (default :11434)
ollama serve &

# Run Plynf with Ollama as upstream
export PLINTH_PROXY_DEMO_MODE=false
export PLINTH_PROXY_UPSTREAM_BASE_URL="http://localhost:11434"
plinth-proxy
```

Then in your code:

```python
client = OpenAI(api_key="ollama", base_url="http://localhost:7430/v1")
client.chat.completions.create(model="llama3.2", messages=[...])
```

> Tool-call interception works whenever Ollama returns the OpenAI
> `tool_calls` shape. Larger Llama 3 / Qwen / Mistral instruct models do;
> very small models often skip the spec — fall back to the SDK in that
> case.

---

## 5. vLLM (self-hosted inference)

vLLM exposes a pure OpenAI server on `:8000/v1` by default:

```bash
python -m vllm.entrypoints.openai.api_server \
    --model meta-llama/Meta-Llama-3.1-8B-Instruct \
    --port 8000 &

export PLINTH_PROXY_UPSTREAM_BASE_URL="http://localhost:8000"
plinth-proxy
```

Identical wiring to Ollama — vLLM is the production version of the same
pattern.

---

## 6. AWS Bedrock Agents (via webhook)

Bedrock action groups call HTTP endpoints. Point them at Plynf's webhook:

```yaml
# Bedrock action-group OpenAPI snippet
paths:
  /v1/tools/get_order/invoke:
    post:
      operationId: getOrder
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              properties:
                arguments:
                  type: object
                  properties:
                    order_id: {type: string}
      responses:
        "200":
          description: shaped order
```

Set the action-group's invocation endpoint to `https://app.plynf.com` and
your Bedrock agent gets shaped tool responses for free. The Plynf savings
dashboard groups these by `agent_id` if you pass it in the body.

---

## 7. LangChain (Python)

Two ways:

**a) Use the Plynf proxy as your LLM base URL** — works if your tools are
registered Plynf connectors, and you let the LLM (via proxy) execute them:

```python
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    base_url="https://app.plynf.com/v1",
    api_key="sk-...",
    model="gpt-4o",
)
```

**b) Wrap client-side tools with `plynf.proxy_client.wrap_tools`** — works
when LangChain executes tools locally and you still want shaping:

```python
from langchain.tools import StructuredTool
from plinth.proxy_client import wrap_tools

def get_order(order_id: str) -> dict:
    return your_erp_client.get_order(order_id)

shaped = wrap_tools(
    [get_order],
    plynf_url="https://app.plynf.com",
    api_key="pl-...",
)[0]

tool = StructuredTool.from_function(shaped, name="get_order")
```

---

## 8. LlamaIndex / CrewAI / AutoGen

Same patterns as LangChain — either use the proxy as `base_url`, or wrap
individual tool callables with `wrap_tool`. The SDK doesn't import any
framework, so it stays small.

```python
from plinth.proxy_client import wrap_tool
from crewai_tools import tool as crewai_tool

@crewai_tool("Get order")
def get_order(order_id: str) -> dict:
    return wrap_tool(
        raw_get_order, plynf_url="https://app.plynf.com", api_key="pl-..."
    )(order_id=order_id)
```

---

## 9. n8n / Zapier / Make

Use the marketplace nodes (n8n: `n8n-nodes-plynf`; Zapier / Make: search
"Plynf"). Drag-and-drop, no code.

---

## 10. Custom Python / Node agent

If you don't use any of the above, hit the webhook directly:

```bash
curl -X POST https://app.plynf.com/v1/tools/get_order/invoke \
  -H "Authorization: Bearer pl-..." \
  -H "Content-Type: application/json" \
  -d '{"arguments": {"order_id": "12345"}}'
```

Returns shaped result + savings metadata in one round trip.

---

## What works in MOCK mode (local development)

When `PLINTH_PROXY_DEMO_MODE=true` (default), Plynf serves a deterministic
mock LLM and mock connectors. Useful for:

- Writing demo scripts and benchmarks
- Iterating on a policy YAML without running OpenAI bills
- CI tests against the OpenAI-compatible contract

Switch to a real upstream by setting both:

```bash
PLINTH_PROXY_DEMO_MODE=false
PLINTH_PROXY_UPSTREAM_BASE_URL=https://api.openai.com
PLINTH_PROXY_UPSTREAM_API_KEY=sk-...
```

---

## Provider matrix

| Provider | How Plynf attaches | Tool-call shaping | Notes |
|---|---|---|---|
| OpenAI | `base_url` swap | Full | Reference implementation |
| Azure OpenAI | Plynf forwards | Full | Set `UPSTREAM_BASE_URL` to Azure deployment URL |
| Anthropic Claude | MCP server (today) / `/v1/messages` (P6) | Full | MCP mode works now |
| Google Gemini / Vertex AI | webhook + adapter (Sprint 3) | Webhook only | Native proxy still TBD |
| AWS Bedrock | webhook (action group) | Full via webhook | OpenAPI YAML provided above |
| Ollama | `base_url` chain | Full | Local dev sweet spot |
| vLLM | `base_url` chain | Full | Self-hosted production |
| LangChain | proxy or SDK wrap | Full | Both patterns supported |
| LlamaIndex / CrewAI / AutoGen | SDK wrap | Full | `wrap_tool` per tool |
| n8n / Zapier / Make | marketplace node | Full | Drag-and-drop |
| Custom Python / Node | webhook | Full | One HTTP call |
| Salesforce Agentforce | APEX action (Sprint 3+) | Planned | Enterprise tier |
| Microsoft Copilot Studio | Power Platform connector (Sprint 3+) | Planned | Enterprise tier |
