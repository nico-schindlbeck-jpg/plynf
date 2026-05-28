# Customer-Support Demo

Shows what Plynf does to a real-world-shaped tool response.

## The setup

A customer-support agent asks: *"What's the status of order #12345?"*

The order database (Salesforce, SAP, custom ERP — pick one) returns a 150-field
JSON blob: customer details, payment metadata, warehouse picker IDs, audit
logs, internal flags, history events. The agent needs eight of those fields.

Without Plynf, all 150 fields enter the LLM's input context. With Plynf,
the `orders.default.yaml` policy filters the response down to:

```
order_id · customer_name · status · tracking_number ·
estimated_delivery · carrier · items_summary · last_status_update
```

…and reports exactly how many tokens, dollars, and exposed fields you saved.

## Run it (offline, no API keys)

```bash
python examples/customer-support/run_demo.py
```

Output (real tokens via tiktoken `o200k_base`):

```
════════════════════════════════════════════════════════════════════════
 Plynf · Customer Support Demo · get_order
════════════════════════════════════════════════════════════════════════
 Tokenizer:                  tiktoken (o200k_base)
 Model:                      gpt-4o ($5.00/1M input tokens)
────────────────────────────────────────────────────────────────────────
 Raw response tokens:               2,478
 Shaped response tokens:              132
 Saved tokens:                      2,346
 Savings:                          94.67%
────────────────────────────────────────────────────────────────────────
 Cost before:                $ 0.012390
 Cost after:                 $ 0.000660
 Cost saved (per call):      $ 0.011730
────────────────────────────────────────────────────────────────────────
 Fields removed (approx):           160
 Kept fields:                order_id, customer_name, status, …
════════════════════════════════════════════════════════════════════════
 Projection at 1,000 calls/day:   $351.90 / month
════════════════════════════════════════════════════════════════════════
```

(Actual numbers depend on your tiktoken install — run it yourself.)

## Run it against the live proxy

In one terminal:

```bash
cd services/proxy
pip install -e ".[dev]"
plinth-proxy
```

In another:

```bash
python examples/customer-support/run_demo.py --proxy http://localhost:7430
```

The proxy will fetch the (mock) order, run the policy, log a savings event,
then call the mock LLM with the shaped response and return an answer like:

> *"Order #12345 is currently 'in_transit'. It's being shipped by DHL
> (tracking: DHL-99887766554433). Estimated delivery: 2026-05-28. Let me
> know if you need me to file a delay claim."*

## Files

- `get_order.json` — raw tool response (150 fields, the kind your ERP
  actually returns).
- `shaped_order_response.json` — what `run_demo.py` produces after
  policy filtering. Regenerated every run.
- `demo_request.json` — OpenAI-compatible chat-completions request that
  triggers the tool call.
- `demo_report.json` — machine-readable savings report from the last run.
- `run_demo.py` — runner.
