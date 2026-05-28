# plinth-proxy

OpenAI-compatible LLM proxy with tool-call interception and a declarative
policy engine. The proxy sits between an AI agent and the LLM provider,
catches tool calls, executes them against Plynf connectors, and **shapes the
tool response** before it ever enters the model's context window.

## Why

A single Salesforce `get_lead` call returns ~4,200 tokens of JSON. The agent
typically needs 8 of the 200 fields. Plynf strips the rest at the boundary —
input tokens drop 80–90 %, the LLM bill drops with them, and 192 fields of
sensitive data never cross to the model provider.

## Endpoints

- `POST /v1/chat/completions` — OpenAI-compatible.
- `GET  /healthz` — liveness probe.
- `GET  /v1/policies` — list loaded connector policies.
- `GET  /v1/savings/summary` — aggregate dashboard view.

## Run locally (mock mode, no API key)

```bash
cd services/proxy
pip install -e ".[dev]"
plinth-proxy            # starts on :7430
# or
python -m plinth_proxy
```

Send a request:

```bash
curl -s http://localhost:7430/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o",
    "messages": [
      {"role": "user", "content": "What is the status of order #12345?"}
    ],
    "tools": [
      {"type": "function", "function": {"name": "get_order", "parameters": {}}}
    ]
  }'
```

You'll get back a fully OpenAI-shaped response, *and* the proxy will have
fetched the (mock) order, shaped it via the `orders.default.yaml` policy,
and logged a savings event you can inspect:

```bash
curl -s http://localhost:7430/v1/savings/summary | jq
```

## Run with real OpenAI upstream

```bash
export PLINTH_PROXY_UPSTREAM_BASE_URL="https://api.openai.com"
export PLINTH_PROXY_UPSTREAM_API_KEY="sk-..."
export PLINTH_PROXY_DEMO_MODE=false
plinth-proxy
```

Then point any OpenAI SDK at the proxy:

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:7430/v1", api_key="sk-anything")
```

## Policies

Shipped in `src/plinth_proxy/policies/`. Each file describes one connector:

```yaml
connector: salesforce
defaults:
  strip_metadata: true
  cache_ttl: 30
tools:
  get_lead:
    allow_fields: [Id, FirstName, LastName, Email, Status, Company]
    redact_pii: {fields: [Email, Phone], mode: hash}
    cache_ttl: 60
```

Override per tenant via `PLINTH_PROXY_POLICIES_DIR=/etc/plinth/policies`.

## Limits — be honest about what the proxy can and can't see

- **Works fully** when the agent uses the OpenAI tool-calling spec and the
  tool name maps to a registered Plynf connector. Proxy runs the tool,
  shapes the response, re-calls the LLM.
- **Limited wins** when the client (LangChain, CrewAI, ...) executes tools
  *client-side* and merely appends the result as a `tool` message — the
  proxy can still trim the inbound payload, but can't intercept the actual
  tool execution. Use the Python SDK for that case.
- **No effect** for server-side tools that run inside the LLM provider
  (e.g. OpenAI Assistants `code_interpreter`, `file_search`) — those don't
  cross the proxy boundary at all.

## Tests

```bash
cd services/proxy
pytest -q
```
