# Example 06 — LLM research agent

A tiny research agent that exercises Plynf's v1.2 `client.llm`
namespace end-to-end. Mirrors example 01's state-externalising pattern
but every reasoning step goes through the SDK's new LLM facade — you
can swap providers (Anthropic, OpenAI, MockProvider) without changing
agent code.

## Running

The default mode uses a deterministic `MockProvider` so the demo runs
with no API keys and no network egress:

```bash
python examples/06-llm-research-agent/compare.py --topic "renewable energy"
```

To call the real Anthropic API:

```bash
ANTHROPIC_API_KEY=sk-ant-... python examples/06-llm-research-agent/compare.py \
    --topic "renewable energy" --mode live
```

To call OpenAI:

```bash
OPENAI_API_KEY=sk-... python examples/06-llm-research-agent/compare.py \
    --topic "renewable energy" --mode openai
```

## What it shows

- `client.llm.use_provider("mock", responses=[...])` — wire a provider
  in one line.
- `response = client.llm.complete(model=..., messages=...)` — synchronous
  completion with token counts and cost on the response object.
- Audit-event recording — when the gateway is reachable each call is
  POSTed to `/v1/audit/record-llm` and the returned `audit_id` is
  attached to the response.
- Provider-agnostic agent code — flipping `--mode` swaps Anthropic /
  OpenAI / Mock without touching any business logic.
