# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Shared building blocks for the multi-agent-handoff demo.

The four modules ``researcher.py``, ``writer.py``, ``reviewer.py``, and
``orchestrate.py`` import from here. The point is the same as in
``01-research-agent/shared.py``: by sharing fixtures, the mock LLM,
prompt templates, and the token counter we keep the demo apples-to-apples
across modes (simulation vs. live) and across runs.

Three things in this module deserve highlighting:

1. **Plinth facade with graceful fallback.** :func:`get_plinth_pipeline`
   returns either a real-services pipeline (workspace + channels through
   the SDK) or an in-process simulation. The agents do not care which
   one they got — both expose the same ``ws.kv`` / ``ws.files`` /
   ``ws.snapshot`` / ``ws.channels`` interface.

2. **Mock LLM.** Three new purposes for the multi-agent demo
   (``draft``, ``critique``, ``finalize``) on top of the v0.1 set
   (``extraction``, ``synthesis``, ``short``). All are deterministic
   given input + topic so the demo is regression-safe in CI.

3. **Per-agent record.** :class:`AgentRecord` accumulates token counts,
   tool calls, and snapshots for one agent. The orchestrator collects
   three of these and assembles a :class:`PipelineReport`.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

try:
    import tiktoken

    _ENC = tiktoken.get_encoding("cl100k_base")
except ImportError as exc:  # pragma: no cover - install-time error
    raise SystemExit("tiktoken is required. Install with: pip install tiktoken") from exc


# ---------------------------------------------------------------------------
# Service endpoints (only consulted in non-simulation modes)
# ---------------------------------------------------------------------------

WORKSPACE_URL = os.environ.get("PLINTH_WORKSPACE_URL", "http://localhost:7421")
GATEWAY_URL = os.environ.get("PLINTH_GATEWAY_URL", "http://localhost:7422")
MOCK_MCP_URL = os.environ.get("PLINTH_MOCK_MCP_URL", "http://localhost:7423")


# ---------------------------------------------------------------------------
# Pricing (Anthropic Sonnet, USD per 1M tokens)
# ---------------------------------------------------------------------------

SONNET_INPUT_USD_PER_MTOK = 3.0
SONNET_OUTPUT_USD_PER_MTOK = 15.0


def count_tokens(text: str) -> int:
    """Return the cl100k_base token count of ``text``."""
    if not text:
        return 0
    return len(_ENC.encode(text))


def estimate_cost(input_tokens: int, output_tokens: int) -> float:
    """Estimate USD cost at Anthropic Sonnet pricing."""
    return (
        input_tokens * SONNET_INPUT_USD_PER_MTOK / 1_000_000
        + output_tokens * SONNET_OUTPUT_USD_PER_MTOK / 1_000_000
    )


def slugify(value: str) -> str:
    """Filesystem/url-safe slug for a topic or URL."""
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value or "untitled"


# ---------------------------------------------------------------------------
# Fixtures — re-use the bundled sources from the v0.1 demo so the
# multi-agent pipeline runs offline with identical content.
# ---------------------------------------------------------------------------

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_V01_DIR = os.path.abspath(os.path.join(_THIS_DIR, "..", "01-research-agent"))


def _load_v01_shared() -> Any:
    spec = importlib.util.spec_from_file_location(
        "_demo01_shared", os.path.join(_V01_DIR, "shared.py")
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not locate v0.1 shared at {_V01_DIR}/shared.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["_demo01_shared"] = module
    spec.loader.exec_module(module)
    return module


_v01_shared = _load_v01_shared()
get_fixture_sources = _v01_shared.get_fixture_sources
_FACT_BANK: list[str] = _v01_shared._FACT_BANK


# ---------------------------------------------------------------------------
# Mock LLM
# ---------------------------------------------------------------------------


def _stable_seed(text: str) -> int:
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)


def _last_user_text(history: list[tuple[str, str]]) -> str:
    for role, content in reversed(history):
        if role == "user":
            return content
    return ""


def _pick(seed: int, items: list[Any], n: int) -> list[Any]:
    chosen: list[Any] = []
    used: set[int] = set()
    for i in range(n):
        idx = (seed + i * 31) % len(items)
        offset = 0
        while idx in used and offset < len(items):
            offset += 1
            idx = (seed + i * 31 + offset) % len(items)
        used.add(idx)
        chosen.append(items[idx])
    return chosen


_FACT_TEMPLATES = [
    "Key facts:\n- {a1}\n- {a2}\n- {a3}\n- {a4}\n- {a5}\n",
    "Extracted findings:\n1. {a1}\n2. {a2}\n3. {a3}\n4. {a4}\n",
    "Summary of source:\n* {a1}\n* {a2}\n* {a3}\n",
]


def _make_extraction_response(history: list[tuple[str, str]]) -> str:
    user_text = _last_user_text(history)
    seed = _stable_seed(user_text)
    template = _FACT_TEMPLATES[seed % len(_FACT_TEMPLATES)]
    facts = _pick(seed, _FACT_BANK, 5)
    body = template.format(
        a1=facts[0],
        a2=facts[1],
        a3=facts[2],
        a4=facts[3],
        a5=facts[4],
    )
    return (
        body
        + "\nThese findings reflect the current consensus as represented in the source. "
        "Confidence: high. Areas of dispute: cost trajectories, geopolitical exposure, "
        "and the pace of regulatory adaptation."
    )


def _make_draft_response(history: list[tuple[str, str]], topic: str) -> str:
    """Generate a deterministic ~600-token markdown first draft."""
    user_text = _last_user_text(history)
    seed = _stable_seed(user_text + topic)
    facts = _pick(seed, _FACT_BANK, 8)
    return f"""# Research report (draft): {topic}

## Executive summary

This draft synthesises findings on **{topic}** drawn from five primary sources.
Several themes emerge consistently and merit highlighting for stakeholders
weighing investment, policy, or operational decisions in this space.

## Key findings

1. {facts[0]}.
2. {facts[1]}.
3. {facts[2]}.
4. {facts[3]}.
5. {facts[4]}.

## Detailed analysis

The first observation is that **{facts[0].lower()}**. Multiple sources
corroborate this with quantitative evidence pointing to the same conclusion.
The implication for practitioners is that strategic planning needs to assume
a materially different cost structure than was prevalent five years ago.

The second observation is that **{facts[1].lower()}**. This concentration
creates both efficiency benefits — economies of scale, learning effects —
and fragility, in that disruptions to the dominant clusters could affect
global supply.

The third observation, that **{facts[2].lower()}**, is closely linked to
the fourth: **{facts[3].lower()}**. Together these point to an environment
where political and regulatory factors are at least as decisive as pure
technology or unit-economics considerations.

A further observation worth highlighting is **{facts[5].lower()}**, which
serves as a counterweight to the headline narrative of rapid progress.

## Cross-source synthesis

The most striking pattern across the five sources is the consistency of
the direction of change paired with substantial disagreement about pace.
All sources agree that the trajectory of {topic} is one of significant
transformation; they differ on timeline, on which actors will lead, and
on which sub-segments will see the steepest changes.

## Recommendations

For decision-makers, the practical recommendations are: track cost-curve
indicators on a quarterly cadence; build optionality into supply-chain
strategy given the geopolitical concentration risks identified above; and
maintain active engagement with policy developments at both subnational
and national levels.
"""


def _make_critique_response(history: list[tuple[str, str]], topic: str) -> str:
    """Generate a deterministic ~250-token critique listing 3-5 issues."""
    user_text = _last_user_text(history)
    seed = _stable_seed("critique:" + user_text + topic)
    issues = _pick(
        seed,
        [
            "Executive summary is generic; it should call out the strongest single finding by name",
            "Recommendations section lacks concrete actions tied to specific numerical thresholds",
            "Cross-source synthesis under-weights the disagreements among sources",
            "The detailed analysis paragraph runs together findings that deserve separate framing",
            "No explicit confidence-level disclosure for the headline claims",
            "Section transitions could highlight the through-line for skim-readers",
            "Missing a 'what would change my mind' counter-evidence section",
            "Citations should appear inline at the point of each claim rather than only in a list",
        ],
        5,
    )
    return f"""## Reviewer critique

After reading the draft on **{topic}**, I see five concrete issues that
should be addressed before this is shippable:

1. {issues[0]}.
2. {issues[1]}.
3. {issues[2]}.
4. {issues[3]}.
5. {issues[4]}.

Beyond these specifics, the draft is structurally sound. The findings
are well-organised, and the cross-source synthesis correctly identifies
the consensus-with-disagreement pattern.
"""


def _make_finalize_response(history: list[tuple[str, str]], topic: str) -> str:
    """Generate a deterministic ~700-token finalised report."""
    user_text = _last_user_text(history)
    seed = _stable_seed("finalize:" + user_text + topic)
    facts = _pick(seed, _FACT_BANK, 8)
    return f"""# Research report: {topic}

> *Confidence: high on structural claims (consensus across 5 sources);
> moderate on pace and sequencing (sources disagree on timeline).*

## Executive summary

The single most important finding from the literature on **{topic}** is
that **{facts[0].lower()}**. This dominates the strategic calculus for
investors, regulators, and operators across the value chain over the
next decade. Four further findings — concentration, policy leverage,
regulatory binding constraints, and the counter-narrative on pace —
qualify and contextualise the headline.

## Key findings (with confidence)

1. **{facts[0]}** *(high confidence — corroborated by five independent
   sources with quantitative evidence)*.
2. **{facts[1]}** *(high confidence — well-documented across sources)*.
3. **{facts[2]}** *(moderate — sources agree on direction, disagree on
   pace)*.
4. **{facts[3]}** *(moderate — politically contingent)*.
5. **{facts[4]}** *(emerging — sources flag this as a watch-item rather
   than a settled conclusion)*.

## Detailed analysis

The first finding — that **{facts[0].lower()}** — is the most consequential
for downstream planning. The cost structure has shifted by an order of
magnitude versus five years ago, which means strategy documents written
on prior assumptions are likely to be miscalibrated.

The concentration story (**{facts[1].lower()}**) creates a duality: the
benefits of scale and learning-curve effects are real, but so is the
fragility of dependence on a small number of jurisdictions and firms.

Finding three (**{facts[2].lower()}**) and finding four
(**{facts[3].lower()}**) together describe the political-economy
substrate. Pure technology and unit-economics analysis is necessary but
not sufficient; political durability is the binding constraint over a
multi-decade horizon.

A counter-narrative worth taking seriously is **{facts[5].lower()}**.
This deserves explicit consideration in any base-case scenario rather
than relegation to a footnote.

## Cross-source synthesis

The five sources agree on the direction of change with high confidence.
They disagree on pace, sequencing, and which actors will be the primary
beneficiaries. A reader seeking certainty on the fast-track scenarios
will find the consensus weaker than the consensus on direction itself.

## Recommendations

For decision-makers operating in or adjacent to {topic}:

- **Track cost-curve indicators quarterly.** Set a numerical threshold
  for triggering strategy revision (e.g., 15% deviation from the central
  scenario).
- **Build supply-chain optionality.** The geopolitical concentration risk
  identified by multiple sources is real; second-source qualification
  should be a planning prerequisite for new commitments.
- **Engage in policy development at multiple levels.** Subnational and
  national policy actions both matter, often on different timelines.

## What would change my mind

A sustained reversal of the cost-curve trend, a major regulatory rollback
in two or more leading jurisdictions, or the emergence of a disruptive
alternative technology not yet visible in the literature.
"""


def _make_short_response(_history: list[tuple[str, str]]) -> str:
    return "Acknowledged. Proceeding."


def call_mock_llm(
    history: list[tuple[str, str]],
    *,
    purpose: str = "extraction",
    topic: str = "",
) -> str:
    """Return a deterministic LLM-style response shaped by ``purpose``."""
    if purpose == "draft":
        return _make_draft_response(history, topic)
    if purpose == "critique":
        return _make_critique_response(history, topic)
    if purpose == "finalize":
        return _make_finalize_response(history, topic)
    if purpose == "extraction":
        return _make_extraction_response(history)
    if purpose == "short":
        return _make_short_response(history)
    return _make_extraction_response(history)


def call_anthropic_llm(
    history: list[tuple[str, str]],
    *,
    purpose: str,
    topic: str,
) -> str:
    """Real Anthropic Sonnet call. Used only in ``--mode=live``."""
    try:
        from anthropic import Anthropic  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised manually
        raise SystemExit(
            "Live mode requires the anthropic package. Install with: "
            "pip install 'plinth-example-multi-agent-handoff[live]'"
        ) from exc

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit("Live mode requires ANTHROPIC_API_KEY env var.")

    client = Anthropic(api_key=api_key)
    messages = [
        {"role": role if role != "tool" else "user", "content": content}
        for role, content in history
        if role in ("user", "assistant", "tool")
    ]
    if not messages or messages[-1]["role"] != "user":
        messages.append({"role": "user", "content": "Continue."})

    system = {
        "extraction": "Extract 3-5 key facts from the source material provided.",
        "draft": f"Write a 500-1000 word markdown first-draft report on '{topic}'.",
        "critique": "Critique the draft. Return 3-5 concrete, actionable issues.",
        "finalize": "Revise the draft to address the critique. Return the full revised report.",
        "short": "Respond briefly with an acknowledgement.",
    }.get(purpose, "Respond helpfully.")

    response = client.messages.create(
        model="claude-3-5-sonnet-latest",
        max_tokens=2500 if purpose in ("draft", "finalize") else 800,
        system=system,
        messages=messages,
    )
    return "".join(
        block.text for block in response.content if getattr(block, "type", "") == "text"
    )


# ---------------------------------------------------------------------------
# Token / call accounting
# ---------------------------------------------------------------------------


def history_to_prompt(history: list[tuple[str, str]]) -> str:
    """Flatten chat history into one tokenisable text blob."""
    parts = [f"<{role}>\n{content}\n</{role}>" for role, content in history]
    return "\n".join(parts)


@dataclass
class LLMCallRecord:
    step: str
    prompt_tokens: int
    response_tokens: int
    duration_ms: int = 0


@dataclass
class ToolCallRecord:
    tool: str
    arguments: dict[str, Any]
    cached: bool
    duration_ms: int


@dataclass
class SnapshotRecord:
    id: str
    name: str
    agent: str


@dataclass
class AgentRecord:
    """Per-agent token + tool-call accounting for one pipeline run."""

    name: str
    llm_calls: list[LLMCallRecord] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    snapshots: list[SnapshotRecord] = field(default_factory=list)

    @property
    def total_input_tokens(self) -> int:
        return sum(c.prompt_tokens for c in self.llm_calls)

    @property
    def total_output_tokens(self) -> int:
        return sum(c.response_tokens for c in self.llm_calls)

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens

    @property
    def total_cost_usd(self) -> float:
        return estimate_cost(self.total_input_tokens, self.total_output_tokens)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_tokens": self.total_tokens,
            "total_cost_usd": self.total_cost_usd,
            "llm_calls": [
                {
                    "step": c.step,
                    "prompt_tokens": c.prompt_tokens,
                    "response_tokens": c.response_tokens,
                    "duration_ms": c.duration_ms,
                }
                for c in self.llm_calls
            ],
            "tool_calls": [
                {
                    "tool": c.tool,
                    "arguments": c.arguments,
                    "cached": c.cached,
                    "duration_ms": c.duration_ms,
                }
                for c in self.tool_calls
            ],
            "snapshots": [
                {"id": s.id, "name": s.name, "agent": s.agent} for s in self.snapshots
            ],
        }


def agent_llm_call(
    history: list[tuple[str, str]],
    *,
    step: str,
    purpose: str,
    topic: str,
    mode: str,
    record: AgentRecord,
) -> str:
    """Single LLM-dispatch entry point that records token usage."""
    prompt = history_to_prompt(history)
    prompt_tokens = count_tokens(prompt)
    start = time.perf_counter()
    if mode == "live":
        try:
            response = call_anthropic_llm(history, purpose=purpose, topic=topic)
        except SystemExit as exc:
            print(f"[live mode unavailable] {exc}; falling back to simulation")
            response = call_mock_llm(history, purpose=purpose, topic=topic)
    else:
        response = call_mock_llm(history, purpose=purpose, topic=topic)
    duration_ms = int((time.perf_counter() - start) * 1000)
    response_tokens = count_tokens(response)
    record.llm_calls.append(
        LLMCallRecord(
            step=step,
            prompt_tokens=prompt_tokens,
            response_tokens=response_tokens,
            duration_ms=duration_ms,
        )
    )
    return response


# ---------------------------------------------------------------------------
# Service-availability detection
# ---------------------------------------------------------------------------


def services_available() -> dict[str, bool]:
    """Probe each Plinth service. Returns map of name → reachable."""
    out = {"workspace": False, "gateway": False, "mock_mcp": False}
    for name, url in (
        ("workspace", WORKSPACE_URL),
        ("gateway", GATEWAY_URL),
        ("mock_mcp", MOCK_MCP_URL),
    ):
        try:
            with httpx.Client(timeout=1.0) as client:
                r = client.get(f"{url}/healthz")
                if r.status_code < 500:
                    out[name] = True
        except Exception:
            pass
    return out


# ---------------------------------------------------------------------------
# Tool-call layer (used by the researcher; falls back to fixtures offline)
# ---------------------------------------------------------------------------


class FixtureToolBackend:
    """In-process backend that serves the bundled v0.1 fixtures."""

    def search(self, query: str, k: int = 5) -> dict[str, Any]:
        sources = get_fixture_sources(query)[:k]
        return {
            "results": [
                {"url": s["url"], "title": s["title"], "snippet": s["snippet"]}
                for s in sources
            ]
        }

    def fetch(self, url: str) -> dict[str, Any]:
        for sources in _v01_shared._FIXTURES.values():
            for src in sources:
                if src["url"] == url:
                    return {
                        "content": src["content"],
                        "status": 200,
                        "content_type": "text/markdown",
                    }
        # Fallback for synthesised URLs.
        match = re.match(r"mock://.+-(\d+)$", url)
        if match:
            idx = int(match.group(1)) - 1
            base = _v01_shared._FIXTURES["renewable energy"]
            if 0 <= idx < len(base):
                return {
                    "content": base[idx]["content"],
                    "status": 200,
                    "content_type": "text/markdown",
                }
        return {
            "content": f"[mock fallback] {url}",
            "status": 200,
            "content_type": "text/plain",
        }


# ---------------------------------------------------------------------------
# In-process workspace + channel bus (fallback when services are down)
# ---------------------------------------------------------------------------


@dataclass
class _SimSnapshot:
    id: str
    name: str
    message: str | None
    created_at: float


@dataclass
class _SimMessage:
    """Mirrors the shape of ``plinth.ChannelMessage`` for the in-process bus."""

    id: str
    channel: str
    workspace_id: str
    seq: int
    payload: Any
    sender: str | None
    type: str | None
    sent_at: str
    delivered_at: str | None = None


class _SimKV:
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    def set(self, key: str, value: Any) -> Any:
        self._data[key] = value
        return value

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)


class _SimFiles:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def write(self, path: str, content: str | bytes, **_kwargs: Any) -> None:
        if isinstance(content, bytes):
            content = content.decode("utf-8")
        self._data[path] = content

    def read(self, path: str, *, as_text: bool = True) -> str | bytes:
        text = self._data.get(path, "")
        return text if as_text else text.encode("utf-8")


class _SimChannels:
    """Dict-backed channel bus that mirrors ``ws.channels`` semantics."""

    def __init__(self, workspace_id: str) -> None:
        self._workspace_id = workspace_id
        self._messages: list[_SimMessage] = []
        self._cursors: dict[str, int] = {}
        self._next_seq: dict[str, int] = {}

    def send(
        self,
        channel: str,
        *,
        payload: Any,
        sender: str | None = None,
        type: str | None = None,  # noqa: A002 - mirrors API
        correlation_id: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> _SimMessage:
        seq = self._next_seq.get(channel, 0) + 1
        self._next_seq[channel] = seq
        msg = _SimMessage(
            id=f"msg_{uuid.uuid4().hex[:24]}",
            channel=channel,
            workspace_id=self._workspace_id,
            seq=seq,
            payload=payload,
            sender=sender,
            type=type,
            sent_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        self._messages.append(msg)
        return msg

    def receive(
        self,
        channel: str,
        *,
        consumer: str | None = None,
        limit: int | None = None,
        peek: bool = False,
        since: int | None = None,
    ) -> list[_SimMessage]:
        if since is not None:
            base = since
        elif consumer is not None:
            base = self._cursors.get(f"{channel}:{consumer}", 0)
        else:
            base = 0
        out: list[_SimMessage] = []
        for msg in self._messages:
            if msg.channel != channel or msg.seq <= base:
                continue
            out.append(msg)
            if limit is not None and len(out) >= limit:
                break
        if out and consumer is not None and not peek:
            self._cursors[f"{channel}:{consumer}"] = out[-1].seq
        return out

    def ack(
        self,
        channel_or_message: str | _SimMessage,
        message_id: str | _SimMessage | None = None,
    ) -> None:
        if isinstance(channel_or_message, _SimMessage):
            target_id = channel_or_message.id
        elif isinstance(message_id, _SimMessage):
            target_id = message_id.id
        elif isinstance(message_id, str):
            target_id = message_id
        else:
            raise TypeError("ack requires a message id")
        self._messages = [m for m in self._messages if m.id != target_id]

    def list(self) -> list[Any]:  # pragma: no cover - convenience parity only
        seen: dict[str, int] = {}
        for msg in self._messages:
            seen[msg.channel] = seen.get(msg.channel, 0) + 1
        return list(seen.keys())

    def message_count(self, channel: str | None = None) -> int:
        if channel is None:
            return len(self._messages)
        return sum(1 for m in self._messages if m.channel == channel)


class InProcessWorkspace:
    """Stand-in for a real :class:`plinth.Workspace` when services are down."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.id = f"ws_sim_{uuid.uuid4().hex[:20]}"
        self.kv = _SimKV()
        self.files = _SimFiles()
        self.channels = _SimChannels(self.id)
        self._snapshots: list[_SimSnapshot] = []

    def snapshot(self, name: str, *, message: str | None = None) -> _SimSnapshot:
        snap = _SimSnapshot(
            id=f"snap_sim_{uuid.uuid4().hex[:20]}",
            name=name,
            message=message,
            created_at=time.time(),
        )
        self._snapshots.append(snap)
        return snap

    def snapshots(self) -> list[_SimSnapshot]:
        return list(self._snapshots)


# ---------------------------------------------------------------------------
# Real Plinth-services pipeline (via the SDK)
# ---------------------------------------------------------------------------


_TOOL_REGISTRATIONS: list[dict[str, Any]] = [
    {
        "tool_id": "web.search",
        "name": "Web search",
        "description": "Search the web; returns a list of source URLs and snippets.",
        "transport": "http",
        "endpoint": "http://localhost:7423/invoke/web.search",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "k": {"type": "integer"}},
            "required": ["query"],
        },
        "output_schema": {"type": "object"},
        "idempotent": True,
        "side_effects": "read",
        "cache_ttl_seconds": 3600,
        "auth_method": "none",
        "auth_config": {},
    },
    {
        "tool_id": "web.fetch",
        "name": "Fetch URL",
        "description": "Fetch a URL and return its text content.",
        "transport": "http",
        "endpoint": "http://localhost:7423/invoke/web.fetch",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        "output_schema": {"type": "object"},
        "idempotent": True,
        "side_effects": "read",
        "cache_ttl_seconds": 3600,
        "auth_method": "none",
        "auth_config": {},
    },
]


def _ensure_tools_registered(client: Any) -> None:
    try:
        existing = {t.tool_id for t in client.tools.list()}
    except Exception:  # noqa: BLE001
        existing = set()
    for reg in _TOOL_REGISTRATIONS:
        if reg["tool_id"] in existing:
            continue
        try:
            client.tools.register(reg)
        except Exception:  # noqa: BLE001 — best-effort
            pass


def _unwrap_invoke(resp: Any) -> dict[str, Any]:
    """Extract the inner tool result from an SDK or simulated response."""
    if hasattr(resp, "result"):
        inner = resp.result
    elif isinstance(resp, dict) and "result" in resp:
        inner = resp["result"]
    else:
        inner = resp
    if isinstance(inner, dict) and "result" in inner and len(inner) == 1:
        inner = inner["result"]
    return inner


@dataclass
class PipelineFacade:
    """Bag-of-stuff that the agents need.

    The orchestrator builds one of these and the agents read from it.
    Building this once at the top of the pipeline lets us avoid having
    each agent independently re-detect services / re-create a workspace.
    """

    workspace: Any            # real plinth.Workspace OR InProcessWorkspace
    workspace_name: str
    workspace_id: str
    sdk_client: Any | None     # real plinth.Plinth, or None
    tool_backend: Any          # FixtureToolBackend (always available)
    mode_label: str            # "sdk" | "simulated"
    services: dict[str, bool]


def get_pipeline_facade(workspace_name: str) -> PipelineFacade:
    """Return a facade hooked up to real services if reachable, else simulated."""
    services = services_available()
    backend = FixtureToolBackend()

    # If all three services are up, try the real SDK path.
    if services["workspace"] and services["gateway"] and services["mock_mcp"]:
        try:
            from plinth import Plinth

            client = Plinth(
                workspace_url=WORKSPACE_URL,
                gateway_url=GATEWAY_URL,
                api_key="local-dev",
            )
            _ensure_tools_registered(client)
            ws = client.workspace(workspace_name)
            return PipelineFacade(
                workspace=ws,
                workspace_name=workspace_name,
                workspace_id=ws.id,
                sdk_client=client,
                tool_backend=backend,
                mode_label="sdk",
                services=services,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[plinth] SDK path failed ({exc}); falling back to simulation.")

    # Simulated path.
    sim = InProcessWorkspace(workspace_name)
    return PipelineFacade(
        workspace=sim,
        workspace_name=workspace_name,
        workspace_id=sim.id,
        sdk_client=None,
        tool_backend=backend,
        mode_label="simulated",
        services=services,
    )


# ---------------------------------------------------------------------------
# wait_for_channel — works with both real SDK and simulated bus
# ---------------------------------------------------------------------------


def wait_for_channel(
    ws: Any,
    channel: str,
    *,
    consumer: str,
    msg_type: str | None = None,
    timeout_s: float = 60.0,
    poll_s: float = 0.2,
) -> Any:
    """Poll ``ws.channels.receive`` until a (matching) message arrives.

    Works with the real SDK :class:`plinth.ChannelsProxy` and with the
    in-process :class:`_SimChannels` simulation, since both expose the
    same ``receive`` and ``ack`` shape.

    Returns the first :class:`ChannelMessage` (real or simulated) of the
    requested type, or ``None`` on timeout.

    The real workspace service raises :class:`ChannelNotFound` for a
    receive on a channel that has never been written to. We treat that
    as "no messages yet" and keep polling until the deadline — channels
    are created lazily on first ``send``.
    """
    deadline = time.monotonic() + max(0.0, timeout_s)
    # Local import so the SDK is optional (simulation mode does not need it).
    try:
        from plinth.exceptions import ChannelNotFound  # type: ignore[attr-defined]
    except ImportError:  # pragma: no cover - SDK present in this repo
        ChannelNotFound = None  # type: ignore[assignment]

    while True:
        try:
            msgs = ws.channels.receive(channel, consumer=consumer, limit=10)
        except Exception as exc:  # noqa: BLE001
            # Treat "channel not yet created" as "no messages yet".
            if (
                ChannelNotFound is not None
                and isinstance(exc, ChannelNotFound)
            ):
                msgs = []
            else:
                raise
        for msg in msgs:
            if msg_type is None or msg.type == msg_type:
                return msg
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll_s)


# ---------------------------------------------------------------------------
# Workspace IO helpers — same call-shape against SDK and simulation
# ---------------------------------------------------------------------------


def kv_get(ws: Any, key: str, default: Any = None) -> Any:
    """ws.kv.get with consistent default behaviour across SDK + simulation."""
    try:
        return ws.kv.get(key, default=default)
    except TypeError:
        # Simulated KV uses positional default.
        return ws.kv.get(key, default)


def files_read_text(ws: Any, path: str) -> str:
    """ws.files.read returning text in both SDK and simulated workspaces."""
    out = ws.files.read(path, as_text=True)
    if isinstance(out, bytes):
        return out.decode("utf-8")
    return out


# ---------------------------------------------------------------------------
# Topic config
# ---------------------------------------------------------------------------


def load_topics_config() -> dict[str, Any]:
    path = os.path.join(_THIS_DIR, "topics.json")
    with open(path) as f:
        return json.load(f)


__all__ = [
    "AgentRecord",
    "FixtureToolBackend",
    "InProcessWorkspace",
    "LLMCallRecord",
    "PipelineFacade",
    "SONNET_INPUT_USD_PER_MTOK",
    "SONNET_OUTPUT_USD_PER_MTOK",
    "SnapshotRecord",
    "ToolCallRecord",
    "_unwrap_invoke",
    "agent_llm_call",
    "call_anthropic_llm",
    "call_mock_llm",
    "count_tokens",
    "estimate_cost",
    "files_read_text",
    "get_pipeline_facade",
    "history_to_prompt",
    "kv_get",
    "load_topics_config",
    "services_available",
    "slugify",
    "wait_for_channel",
]
