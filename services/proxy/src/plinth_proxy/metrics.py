# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Prometheus text-exposition metrics for the proxy.

Plynf's headline value metric is *tokens saved*. This module renders the
in-process savings log (and per-tenant budget usage) into the Prometheus
text exposition format [1] so operators can scrape ``GET /metrics`` into
Grafana / alerting.

We deliberately hand-render the format rather than depend on
``prometheus_client``: the exposition grammar is small and stable, and the
proxy's value is being a thin, low-dependency front door. Adding a metrics
client (plus its registry/process-collector machinery) for the handful of
gauges below would be a poor trade.

Exposed series (all prefixed ``plynf_``):

  build_info{version}            gauge   constant 1; version carried as label
  savings_calls_total            counter tool-call interceptions recorded
  raw_tokens_total               counter raw (pre-shaping) response tokens
  shaped_tokens_total            counter shaped (post-policy) response tokens
  saved_tokens_total             counter tokens saved (raw-shaped + cache hits)
  cost_saved_usd_total           counter model-priced USD saved on input tokens
  cache_hits_total               counter calls served from cache / in-round merge
  savings_ratio                  gauge   saved / raw (0..1)
  cache_hit_ratio                gauge   cache hits / calls (0..1)
  connector_saved_tokens_total   counter saved tokens, partitioned by connector
  tenant_tokens_used             gauge   month-to-date shaped tokens, per tenant

[1] https://prometheus.io/docs/instrumenting/exposition_formats/
"""

from __future__ import annotations

from collections.abc import Mapping

from .savings import SavingsEvent, aggregate

# Prometheus text format v0.0.4. Scrapers content-negotiate on this exact
# string; ``charset=utf-8`` matches what ``prometheus_client`` emits.
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _escape_label_value(value: str) -> str:
    """Escape a label value per the exposition spec.

    Only backslash, double-quote and newline are special inside a label
    value; everything else is passed through verbatim.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _fmt(value: float) -> str:
    """Render a metric value. Ints stay integer-looking; floats are trimmed."""
    if isinstance(value, bool):  # bool is an int subclass — guard before int
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    # Fixed precision then strip trailing zeros so 0.5 -> "0.5", 123.0 -> "123".
    text = f"{float(value):.6f}".rstrip("0").rstrip(".")
    return text or "0"


def _emit(
    lines: list[str],
    name: str,
    metric_type: str,
    help_text: str,
    samples: list[tuple[dict[str, str], float]],
) -> None:
    """Append one metric *family* (HELP + TYPE header, then all samples).

    A family's ``# HELP`` / ``# TYPE`` lines must appear once, before its
    samples, and samples for one name must not be interleaved with another —
    callers therefore pass every sample for ``name`` in a single call.
    """
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} {metric_type}")
    for labels, value in samples:
        if labels:
            label_str = ",".join(
                f'{k}="{_escape_label_value(v)}"' for k, v in labels.items()
            )
            lines.append(f"{name}{{{label_str}}} {_fmt(value)}")
        else:
            lines.append(f"{name} {_fmt(value)}")


def render_metrics(
    events: list[SavingsEvent],
    *,
    version: str,
    tenant_usage: Mapping[str, int] | None = None,
) -> str:
    """Render the savings log + per-tenant usage as Prometheus text.

    ``events`` is the in-memory ``SavingsEvent`` log (``AppState.events``);
    ``tenant_usage`` is ``{tenant_id: tokens_this_month}`` from the tier gate.
    The returned string is a complete exposition (trailing newline included).
    """
    agg = aggregate(events)
    cache_hits = sum(1 for e in events if e.cache_hit)
    lines: list[str] = []

    _emit(
        lines,
        "plynf_build_info",
        "gauge",
        "Build metadata; constant 1 with the version carried as a label.",
        [({"version": version}, 1)],
    )
    _emit(
        lines,
        "plynf_savings_calls_total",
        "counter",
        "Total tool-call interceptions recorded.",
        [({}, agg["total_calls"])],
    )
    _emit(
        lines,
        "plynf_raw_tokens_total",
        "counter",
        "Total raw (pre-shaping) tool-response tokens observed.",
        [({}, agg["total_raw_tokens"])],
    )
    _emit(
        lines,
        "plynf_shaped_tokens_total",
        "counter",
        "Total shaped (post-policy) tool-response tokens emitted.",
        [({}, agg["total_shaped_tokens"])],
    )
    _emit(
        lines,
        "plynf_saved_tokens_total",
        "counter",
        "Total tokens saved (raw minus shaped, plus full raw on cache hits).",
        [({}, agg["total_saved_tokens"])],
    )
    _emit(
        lines,
        "plynf_cost_saved_usd_total",
        "counter",
        "Estimated USD saved on LLM input tokens, model-priced.",
        [({}, agg["total_cost_saved_usd"])],
    )
    _emit(
        lines,
        "plynf_cache_hits_total",
        "counter",
        "Total tool calls served from cache or in-round merge.",
        [({}, cache_hits)],
    )
    _emit(
        lines,
        "plynf_savings_ratio",
        "gauge",
        "Saved tokens divided by raw tokens (0..1) across all calls.",
        [({}, agg["savings_pct"])],
    )
    _emit(
        lines,
        "plynf_cache_hit_ratio",
        "gauge",
        "Fraction of calls served from cache or in-round merge (0..1).",
        [({}, agg["cache_hit_rate"])],
    )

    by_connector = agg["top_connectors_by_savings"]  # [(connector, saved), ...]
    if by_connector:
        _emit(
            lines,
            "plynf_connector_saved_tokens_total",
            "counter",
            "Tokens saved, partitioned by connector.",
            [({"connector": conn}, saved) for conn, saved in by_connector],
        )

    if tenant_usage:
        _emit(
            lines,
            "plynf_tenant_tokens_used",
            "gauge",
            "Shaped tokens charged to a tenant this calendar month (resets monthly).",
            [({"tenant": t}, used) for t, used in sorted(tenant_usage.items())],
        )

    return "\n".join(lines) + "\n"


__all__ = ["CONTENT_TYPE", "render_metrics"]
