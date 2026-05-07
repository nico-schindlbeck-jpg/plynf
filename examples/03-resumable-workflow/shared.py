# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Shared building blocks for the resumable-workflow demo.

This module is the simulation substrate the demo runs on when the
real Plinth services aren't reachable, plus a thin set of pieces
imported into ``workflow_agent.py``:

* :func:`count_tokens` — cl100k_base token counter (Anthropic-compatible).
* :func:`call_mock_llm` — deterministic LLM stub matching the response
  shapes of the production research agent in
  :mod:`examples.01-research-agent.shared`.
* :class:`SimulatedWorkspaceStore` — file-backed in-process workspace
  with KV, files, and snapshots. Used when the workspace service is
  not reachable. State is persisted to JSON under
  ``$PLINTH_DATA_DIR/03-resumable-workflow/`` so subprocess
  invocations of ``workflow_agent.py`` see consistent state across
  restarts.
* :class:`SimulatedWorkflowStore` — file-backed workflow store
  implementing the v0.2 ``Workflows API`` semantics: a manifest of
  ordered steps, append-only step log, and a ``resume_info`` query
  that returns the next pending step plus the most recent snapshot.

The whole point of all this scaffolding is to let the crash-and-resume
demo run end-to-end without any services. With services up, the
simulated stores fall away and the real SDK (with ``ws.workflows``,
``ws.snapshot``, ``ws.kv`` etc.) is used instead.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

try:  # tiktoken is the canonical token counter
    import tiktoken

    _ENC = tiktoken.get_encoding("cl100k_base")
except ImportError as exc:  # pragma: no cover - install-time error
    raise SystemExit(
        "tiktoken is required. Install with: pip install tiktoken"
    ) from exc


# ---------------------------------------------------------------------------
# Pricing (Anthropic Sonnet, USD per 1M tokens)
# ---------------------------------------------------------------------------

SONNET_INPUT_USD_PER_MTOK = 3.0
SONNET_OUTPUT_USD_PER_MTOK = 15.0


# ---------------------------------------------------------------------------
# Service endpoints (consulted only when probing services / using SDK)
# ---------------------------------------------------------------------------

WORKSPACE_URL = os.environ.get("PLINTH_WORKSPACE_URL", "http://localhost:7421")
GATEWAY_URL = os.environ.get("PLINTH_GATEWAY_URL", "http://localhost:7422")
MOCK_MCP_URL = os.environ.get("PLINTH_MOCK_MCP_URL", "http://localhost:7423")


# ---------------------------------------------------------------------------
# Persistent state directory for the simulated stores
# ---------------------------------------------------------------------------

_DEFAULT_DATA_DIR = "/tmp/plinth-data/03-resumable-workflow"
SIM_DATA_DIR = Path(os.environ.get("PLINTH_DEMO3_DATA_DIR", _DEFAULT_DATA_DIR))


def reset_simulation_state(workspace_name: str | None = None) -> None:
    """Wipe simulated state.

    If ``workspace_name`` is given, only that workspace's state files
    are removed. Otherwise the entire SIM_DATA_DIR is wiped.
    """
    if not SIM_DATA_DIR.exists():
        SIM_DATA_DIR.mkdir(parents=True, exist_ok=True)
        return

    if workspace_name is None:
        for child in SIM_DATA_DIR.iterdir():
            _rmtree(child)
        return

    slug = slugify(workspace_name)
    for child in SIM_DATA_DIR.iterdir():
        if child.name.endswith(f"-{slug}.json") or child.name.endswith(
            f"-{slug}.json.tmp"
        ):
            child.unlink()


def _rmtree(path: Path) -> None:
    if not path.exists():
        return
    if path.is_file():
        path.unlink()
        return
    for child in path.iterdir():
        _rmtree(child)
    path.rmdir()


# ---------------------------------------------------------------------------
# Token counting & costing
# ---------------------------------------------------------------------------


def count_tokens(text: str) -> int:
    """Count tokens in ``text`` using cl100k_base (Anthropic-compatible)."""
    if not text:
        return 0
    return len(_ENC.encode(text))


def estimate_cost(input_tokens: int, output_tokens: int) -> float:
    """Estimate USD cost at Anthropic Sonnet pricing."""
    return (
        input_tokens * SONNET_INPUT_USD_PER_MTOK / 1_000_000
        + output_tokens * SONNET_OUTPUT_USD_PER_MTOK / 1_000_000
    )


# ---------------------------------------------------------------------------
# Slugification
# ---------------------------------------------------------------------------


def slugify(value: str) -> str:
    """Make a filesystem-safe slug out of an arbitrary string."""
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower())
    return cleaned.strip("-") or "topic"


# ---------------------------------------------------------------------------
# ULID-ish identifier generator
# ---------------------------------------------------------------------------


def short_ulid() -> str:
    """Return a Crockford-ish base32 26-char identifier (UUID4 hex stub)."""
    return uuid.uuid4().hex[:26].upper()


# ---------------------------------------------------------------------------
# Fixture sources (5 sources reused for any topic)
# ---------------------------------------------------------------------------

_SOURCE_TEMPLATES: list[dict[str, str]] = [
    {
        "url_suffix": "1",
        "title": "Solar Power and the Industrial Cost Curve",
        "snippet": "How photovoltaic technology became the cheapest electricity source.",
        "content": (
            "Solar power has undergone one of the most remarkable cost transformations in "
            "the history of any industrial technology. Between 2010 and 2025, the levelized "
            "cost of electricity from utility-scale photovoltaic installations fell by "
            "approximately 89 percent, dropping from around 28 cents per kilowatt-hour to "
            "under 4 cents in the most favorable markets. This decline was not the product "
            "of a single technological breakthrough but rather the cumulative effect of "
            "incremental improvements compounding through global manufacturing scale.\n\n"
            "The dominant technology, crystalline silicon solar cells, has seen efficiency "
            "gains from roughly 14 percent module efficiency in 2010 to 22-24 percent for "
            "the best commercial modules in 2025. More importantly, manufacturing scale in "
            "China grew the global module production capacity from 25 gigawatts per year to "
            "over 600 gigawatts per year over the same period. The resulting overcapacity "
            "drove relentless price competition, which in turn forced producers to pursue "
            "further automation and bill-of-materials reductions, creating a positive "
            "feedback loop that economists call a learning curve.\n\n"
            "Utility-scale solar farms now span tens of thousands of acres in regions like "
            "the American Southwest, Western Australia, India's Rajasthan, and the Atacama "
            "Desert in Chile. The largest single-site installation as of 2025 exceeds 5 "
            "gigawatts of generating capacity, larger than most nuclear power stations. "
            "These facilities employ tracking systems that follow the sun's path through "
            "the sky, increasing daily generation by 15 to 25 percent compared to "
            "fixed-tilt arrays.\n\n"
            "Distributed generation — rooftop systems on homes, commercial buildings, and "
            "warehouses — represents a parallel revolution. In Australia, more than one in "
            "three single-family homes has rooftop solar. In Germany, the figure is "
            "approaching one in four. In California, new homes built since 2020 must "
            "include solar capacity by law. The economics of self-consumption have "
            "fundamentally restructured electricity bills for these households, with "
            "payback periods now under 6 years in most markets.\n\n"
            "The intermittency problem — solar produces nothing at night and less in "
            "winter — is increasingly addressed through battery storage. Lithium iron "
            "phosphate cells fell 85 percent in cost over the same window. Utilities now "
            "routinely install solar paired with 4-hour battery systems, providing "
            "dispatchable evening peak power. The next frontier is multi-day storage, "
            "addressed through novel chemistries like iron-air batteries and through "
            "long-distance transmission that pools generation across continental areas.\n\n"
            "Materials supply chains present geopolitical concerns. Polysilicon production "
            "is concentrated in a handful of Chinese provinces; silver demand from cell "
            "manufacturing has been one of the largest sources of marginal silver demand "
            "globally. Trade policy responses, including U.S. and EU tariffs on Chinese "
            "modules, have shifted some manufacturing to Southeast Asia and India but have "
            "not displaced China as the dominant supplier.\n\n"
            "Looking forward, perovskite tandem cells promise efficiency gains beyond the "
            "silicon ceiling, with laboratory cells now exceeding 33 percent. Commercial "
            "deployment timelines are uncertain due to durability questions, but several "
            "companies are bringing first products to market in 2025-2026. The "
            "International Energy Agency projects that solar will become the world's "
            "largest single source of electricity generation before 2030, surpassing coal."
        ),
    },
    {
        "url_suffix": "2",
        "title": "Wind Power and the Grid Integration Question",
        "snippet": "Modern wind turbines, transmission constraints, and offshore frontiers.",
        "content": (
            "Wind energy has scaled to over 1 terawatt of installed global capacity by "
            "2025, generating roughly 9 percent of world electricity. The trajectory "
            "mirrors solar in its cost decline but has progressed through different "
            "technology levers. While solar improved through cell-level efficiency and "
            "module-level manufacturing scale, wind progressed primarily through making "
            "turbines bigger.\n\n"
            "The modal onshore turbine in 2010 had a rotor diameter around 80 meters and "
            "a hub height around 80 meters, with a rated capacity of 2 to 3 megawatts. By "
            "2025, the mainstream onshore turbine has rotor diameters of 150 to 170 meters, "
            "hub heights of 130 to 160 meters, and rated capacities of 5 to 7 megawatts "
            "per unit. This is roughly an order of magnitude more energy harvest per "
            "turbine, achieved primarily by sweeping a larger area of the wind resource at "
            "higher altitudes where wind is steadier and faster.\n\n"
            "Offshore wind has gone through an even more dramatic scale-up. The first "
            "commercial offshore wind farm — Vindeby in Denmark, commissioned in 1991 — "
            "used 11 turbines of 450 kilowatts each, a total project size of about 5 "
            "megawatts. The current state of the art for fixed-bottom offshore turbines is "
            "around 18 megawatts per unit, with rotor diameters approaching 250 meters. "
            "Project sizes routinely exceed 1 gigawatt; the largest operational projects "
            "in the North Sea exceed 3 gigawatts.\n\n"
            "Floating offshore wind opens up vast deepwater resources that fixed-bottom "
            "installations cannot reach. Pilot projects off Scotland, Portugal, and "
            "California have demonstrated technical viability. The first commercial-scale "
            "floating projects, in the 100-300 megawatt range, are being commissioned in "
            "2025-2026. The economics of floating wind remain less competitive than "
            "fixed-bottom but are following a learning curve similar to early offshore.\n\n"
            "Grid integration has emerged as the dominant constraint on continued wind "
            "deployment in mature markets. Wind resource is geographically concentrated, "
            "often far from population centers, while grids were designed around "
            "centralized fossil generation near load. Texas, Iowa, and the central United "
            "States produce far more wind power than they can locally consume, and "
            "transmission limits the ability to move that power to demand centers on the "
            "coasts.\n\n"
            "Curtailment — being paid to not generate when local demand is saturated and "
            "transmission is full — has become a significant economic factor in wind-rich "
            "regions. Some Texas wind farms are curtailed for several hundred hours per "
            "year. The mitigation is more transmission, a notoriously difficult permitting "
            "and political problem in the United States and Europe alike.\n\n"
            "Wildlife impacts, particularly bird and bat mortality, remain a real but "
            "addressable concern. Population-level effects on most species are well below "
            "other anthropogenic mortality sources like buildings, vehicles, and "
            "free-roaming cats. Site-specific siting protocols, radar-triggered "
            "curtailment during migration periods, and ultrasonic deterrent systems for "
            "bats are reducing mortality at well-managed sites.\n\n"
            "The next decade is expected to see wind capacity at least double from 2025 "
            "levels, with offshore growing several-fold and floating offshore reaching "
            "first commercial-scale deployment. Constraints will continue to shift from "
            "technology to permitting, transmission, and supply chain bottlenecks rather "
            "than fundamental cost."
        ),
    },
    {
        "url_suffix": "3",
        "title": "Battery Storage and Electricity Markets",
        "snippet": "How lithium-ion and emerging chemistries are enabling 100% renewable grids.",
        "content": (
            "Grid-scale battery storage has gone from a niche pilot technology in 2015 to "
            "one of the largest sources of new generating capacity additions in the United "
            "States by 2024. The driver is straightforward: lithium-ion battery cell prices "
            "fell from around $1100 per kilowatt-hour in 2010 to under $90 per "
            "kilowatt-hour by 2024, a roughly 92 percent decline. The trajectory mirrors "
            "solar's cost curve but compressed into half the time.\n\n"
            "The dominant chemistry for stationary applications is now lithium iron "
            "phosphate, or LFP. LFP cells trade some energy density compared to "
            "nickel-rich chemistries for substantially better cycle life, thermal "
            "stability, and lower raw material cost. For stationary storage, where weight "
            "and volume are less constrained than in vehicles, the trade is favorable. By "
            "2025, more than 70 percent of new utility-scale battery deployments use LFP "
            "cells.\n\n"
            "The most common configuration for utility-scale projects is a 4-hour duration "
            "system: a 100 megawatt power rating with 400 megawatt-hours of energy "
            "storage. This sizing reflects the typical evening peak load profile in "
            "solar-rich grids, where solar production drops at sunset just as residential "
            "air-conditioning and lighting demand peaks. Discharging stored solar energy "
            "across that 3-5 hour window has emerged as a high-value market.\n\n"
            "California's grid is the canonical case study. The state grew from "
            "approximately 250 megawatts of battery storage in 2020 to over 13 gigawatts "
            "by 2024. On many summer evenings, batteries provide more power to the "
            "California grid than natural gas does — a transition that observers in 2018 "
            "considered to be at least a decade away.\n\n"
            "Beyond 4-hour duration, the economics shift significantly. The capital cost "
            "of a battery scales roughly linearly with energy storage capacity, while the "
            "value of additional duration falls off quickly past the typical 4-hour "
            "evening peak. This has motivated research into longer-duration storage "
            "technologies that can store energy more cheaply at the cost of lower power "
            "efficiency.\n\n"
            "Emerging chemistries include iron-air batteries, which store energy through "
            "reversible oxidation of metallic iron. Form Energy, the leading commercial "
            "developer, has begun deploying 100-hour duration systems at electric utility "
            "sites in 2024-2025. The energy capital cost is substantially lower than "
            "lithium-ion but the round-trip efficiency is also lower, around 50 percent "
            "compared to 90 percent for lithium. This trade is favorable for systems that "
            "cycle infrequently — a few times per year, riding out multi-day weather "
            "events that produce sustained low solar and wind output.\n\n"
            "Battery raw material supply chains remain a strategic concern. Lithium, "
            "cobalt, and nickel mining are concentrated in a handful of countries, with "
            "significant reserves in geopolitically sensitive jurisdictions. The shift to "
            "LFP has substantially reduced cobalt and nickel demand for stationary "
            "storage, while lithium remains the critical bottleneck. Lithium prices were "
            "highly volatile through the 2020-2024 period, ranging from below $7,000 per "
            "ton to over $80,000 per ton, before settling in the $10,000-15,000 range in "
            "2025 as new mining capacity came online."
        ),
    },
    {
        "url_suffix": "4",
        "title": "Green Hydrogen: Promise and the Cost Gap",
        "snippet": "The role of electrolysis-derived hydrogen in decarbonizing industry.",
        "content": (
            "Hydrogen produced by electrolyzing water using renewable electricity — green "
            "hydrogen — is widely considered essential for decarbonizing parts of the "
            "economy that cannot be electrified directly: heavy industry like steelmaking, "
            "ammonia production for fertilizers, long-haul shipping, and aviation. As of "
            "2025, however, green hydrogen remains expensive relative to fossil-derived "
            "alternatives and project deployment has lagged announcements.\n\n"
            "The current global production of hydrogen is approximately 95 million tons "
            "per year, used primarily for ammonia synthesis and oil refining. Almost all "
            "of this hydrogen is produced from natural gas via steam methane reforming, a "
            "process that emits roughly 9 to 10 tons of carbon dioxide per ton of hydrogen "
            "produced. Replacing this gray hydrogen with low-carbon alternatives would "
            "eliminate roughly 900 million tons of CO2 per year, comparable to the total "
            "emissions of Germany.\n\n"
            "Electrolyzer technology has two main commercial pathways. Alkaline "
            "electrolyzers are the mature workhorse, using potassium hydroxide solution, "
            "with decades of commercial track record. Proton exchange membrane, or PEM, "
            "electrolyzers are newer, more compact, and more flexible in their response to "
            "variable input power, but use platinum-group metal catalysts and cost more "
            "per kilowatt of capacity. A third technology, solid oxide electrolyzers, "
            "operates at high temperature with potentially higher efficiency but is still "
            "in early commercial deployment.\n\n"
            "The cost of green hydrogen is dominated by two factors: the levelized cost of "
            "the input electricity, and the capital cost of the electrolyzer running at a "
            "low capacity factor. To produce hydrogen at $2 per kilogram, roughly "
            "competitive with steam methane reforming, electricity must cost around $20 "
            "per megawatt-hour and the electrolyzer must be deployed at a capacity factor "
            "approaching 50 percent. As of 2025, both conditions are difficult to meet "
            "simultaneously.\n\n"
            "The Inflation Reduction Act in the United States introduced production tax "
            "credits of up to $3 per kilogram for hydrogen produced with very low carbon "
            "intensity. Final rules issued in 2024 imposed strict requirements on the "
            "hourly matching of renewable electricity to electrolyzer operation, "
            "additionality of new renewable capacity, and geographic deliverability. These "
            "rules, intended to ensure that the hydrogen is genuinely low-carbon, also "
            "raise the cost of qualifying.\n\n"
            "Deployment to date has been slower than announcements suggested. Of the more "
            "than 1,500 hydrogen projects publicly announced globally as of 2024, only a "
            "small fraction had reached final investment decision and far fewer were under "
            "construction. The gap reflects the difficulty of securing offtake agreements "
            "at prices that cover production costs even with subsidy. Industrial buyers "
            "have generally been unwilling to commit to long-term offtake at $5-7 per "
            "kilogram, the typical real cost of green hydrogen in 2024-2025, when their "
            "existing gray hydrogen costs $1-2 per kilogram.\n\n"
            "Steelmaking is one of the highest-leverage applications for green hydrogen. "
            "Direct-reduction of iron ore using hydrogen, in place of coal, produces "
            "sponge iron that can be melted in electric arc furnaces using renewable "
            "electricity. Several full-scale projects are advancing in Sweden, Germany, "
            "and Spain. Hydrogen-based steelmaking emits roughly 80-90 percent less CO2 "
            "than coal-based blast furnace steelmaking. The first commercial green steel "
            "was sold in 2021 at a substantial premium; volumes are growing but remain a "
            "small fraction of global steel production."
        ),
    },
    {
        "url_suffix": "5",
        "title": "Grid Modernization and the Software Layer",
        "snippet": "How the electricity grid must change to handle high renewable penetration.",
        "content": (
            "The electricity grid was designed around centralized fossil-fuel and "
            "hydroelectric generation, with power flowing one direction from large plants "
            "to dispersed consumers. High penetrations of variable renewables, distributed "
            "rooftop generation, and increasingly active demand-side resources require "
            "fundamental changes to how the grid is built and operated. The combined "
            "investment in grid modernization across major economies through 2035 is "
            "projected to exceed $5 trillion.\n\n"
            "Transmission expansion is the single largest physical bottleneck. New "
            "high-voltage lines take a decade or more to permit and build in the United "
            "States, with siting battles, multi-state regulatory approval, and landowner "
            "negotiations creating sustained delays. The Pacific Northwest has "
            "substantial wind and hydro that cannot reach demand in California due to "
            "limited interties; the central United States produces enormous amounts of "
            "wind power that struggles to reach coastal demand.\n\n"
            "High-voltage direct current, or HVDC, transmission is increasingly important "
            "for long-distance power transfer because it reduces line losses and allows "
            "asynchronous interconnection between grids. China has built the world's most "
            "extensive HVDC network, including lines exceeding 3,000 kilometers in length "
            "and 12 gigawatts of capacity. Europe is building substantial HVDC backbones "
            "to integrate North Sea offshore wind. The United States has historically "
            "built less HVDC than its scale would suggest, though several major projects "
            "are advancing.\n\n"
            "At the distribution level, the rise of rooftop solar and behind-the-meter "
            "batteries is transforming traditionally one-way distribution circuits into "
            "bi-directional networks. The fundamental engineering challenge is voltage "
            "management: when distributed solar is producing more than local load, voltage "
            "rises along the feeder, potentially exceeding equipment limits and triggering "
            "inverter trip-offs that create cascading instabilities. The mitigation is "
            "smart inverter technology with grid-supportive functions built in.\n\n"
            "Demand-side flexibility is the third pillar of grid modernization. "
            "Historically, electricity demand was treated as inelastic — utilities had to "
            "match supply to whatever demand the grid presented. With intelligent "
            "thermostats, electric vehicle smart charging, hot water heater control, and "
            "increasingly sophisticated industrial demand response, a meaningful fraction "
            "of demand can be shifted in time. Texas has been a leading market for demand "
            "response programs that exceed 5 gigawatts of dispatchable load reduction.\n\n"
            "Electric vehicle smart charging deserves particular attention. The average "
            "residential EV adds roughly 4-6 kilowatt-hours per day of new electricity "
            "demand, but with managed charging, that demand can be moved into off-peak "
            "hours — particularly midday solar peaks where excess generation would "
            "otherwise be curtailed, or late-night low-load hours. Several pilot programs "
            "have demonstrated that 80-90 percent of EV charging energy can be delivered "
            "during periods of low system stress when smart charging is enabled.\n\n"
            "The software layer underpinning the modern grid is increasingly "
            "safety-critical. Outages in 2003, 2011, and 2021 in North America were each "
            "substantially caused or worsened by software systems that didn't behave as "
            "intended under stress. Cybersecurity is a major concern, with multiple "
            "confirmed and suspected attacks on grid operational technology in the past "
            "decade. Hardening the operational technology of the grid against both "
            "natural disasters and cyberattacks is a substantial parallel investment."
        ),
    },
]


def get_fixture_sources(topic: str) -> list[dict[str, str]]:
    """Return the bundled fixture sources, retitled for ``topic``."""
    slug = slugify(topic)
    return [
        {
            "url": f"mock://{slug}-{tpl['url_suffix']}",
            "title": f"{topic.title()}: {tpl['title']}",
            "snippet": tpl["snippet"],
            "content": tpl["content"],
        }
        for tpl in _SOURCE_TEMPLATES
    ]


# ---------------------------------------------------------------------------
# Mock LLM — deterministic response shapes
# ---------------------------------------------------------------------------


def _stable_seed(text: str) -> int:
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)


def _last_user_text(history: list[tuple[str, str]]) -> str:
    for role, content in reversed(history):
        if role == "user":
            return content
    return ""


_FACT_BANK = [
    "Cost declines on the order of 80-90% over a decade in the dominant technology",
    "Manufacturing scale concentrated in a small number of jurisdictions",
    "Subsidies and tax credits are now the primary policy lever in the U.S. and EU",
    "Grid integration and permitting are emerging as the binding constraints",
    "Storage durations of 4 hours dominate today; longer-duration tech is in early commercial",
    "Workforce and supply chain bottlenecks are slowing announced project pipelines",
    "Emerging markets are scaling deployment at a higher pace than mature markets",
    "Innovation in software and orchestration is matching innovation in hardware",
    "Capital expenditure is concentrated in 5-10 hubs globally",
    "Public-private financing structures are evolving to spread early-mover risk",
    "Regulatory approval timelines exceed 3 years in most jurisdictions",
    "Recycling and circularity now factor into the long-run cost analysis",
    "Industry consolidation is producing 3-5 dominant integrated players per region",
    "Geopolitical concentration of critical inputs is a recurring risk theme",
    "Pricing dynamics are increasingly driven by long-term offtake contracts",
]


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


def _make_extraction_response(history: list[tuple[str, str]]) -> str:
    """Per-source fact extraction. Targeted at ~1500 tokens of output.

    Real Sonnet extractions on a ~1500-token source produce roughly
    300-500 tokens of structured facts. We pad the response with a short
    analysis section per fact so the per-source tokens land closer to
    the working budget noted in the spec.
    """
    user_text = _last_user_text(history)
    seed = _stable_seed(user_text)
    facts = _pick(seed, _FACT_BANK, 8)
    body = (
        "## Key facts from this source\n\n"
        f"1. **{facts[0]}.** This is a quantitative claim grounded in the data the source presents; "
        "the timeline and magnitude of the trend are central to interpreting the rest of the material.\n"
        f"2. **{facts[1]}.** The structural concentration the source describes is itself a recurring theme "
        "across the literature, and the implications for resilience and policy are flagged in the body.\n"
        f"3. **{facts[2]}.** This finding connects upstream conditions to the downstream observations the source draws; "
        "it is treated as the binding variable for how the trend resolves over the next planning horizon.\n"
        f"4. **{facts[3]}.** The source pairs this observation with concrete examples and dates; "
        "the pattern is consistent with what other sources in this domain have reported in adjacent windows.\n"
        f"5. **{facts[4]}.** A second-order implication of the headline finding, this point matters for any planner "
        "trying to translate the source's headline conclusions into operational decisions.\n\n"
        "## Quantitative anchors\n\n"
        f"- {facts[5]}\n"
        f"- {facts[6]}\n"
        f"- {facts[7]}\n\n"
        "## Confidence and caveats\n\n"
        "These findings reflect the consensus represented in the source. The methodological approach is "
        "explicit and reproducible. Confidence: high on the empirical claims; moderate on the forward-looking "
        "projections. Areas where the source's framing is contested elsewhere include cost trajectories, "
        "geopolitical exposure, and the pace of regulatory adaptation. Cross-checking against complementary "
        "sources is advisable before relying on the strongest of these claims for high-stakes decisions.\n\n"
        "## Implications\n\n"
        "For practitioners working on this topic, the most actionable observations are the first two findings, "
        "which set the boundary conditions for any operational planning. The next three findings refine the "
        "picture but do not change the headline conclusion: the trajectory is durable, the binding constraints "
        "have shifted, and the policy environment is the dominant variable in most jurisdictions. "
        "Stakeholders who treat the empirical findings as planning baselines and the policy commentary as "
        "scenario inputs will be using the source as it was intended."
    )
    return body


def _make_outline_response(history: list[tuple[str, str]], topic: str) -> str:
    """Structured outline. Targeted at ~2000 tokens.

    A realistic outline for a long-form report is verbose: section
    descriptions, sub-bullets, source attributions, and notes on what
    each section should accomplish.
    """
    user_text = _last_user_text(history)
    seed = _stable_seed(user_text + topic + "outline")
    facts = _pick(seed, _FACT_BANK, 12)
    return f"""# Detailed outline for: {topic}

## I. Introduction (300-400 words)
- Hook: contextualise {topic} against the broader economic and policy backdrop.
- Thesis sentence: a single line summarising the report's central argument.
- Stakes: why decision-makers should care now rather than five years from now.
- Roadmap: a paragraph forecasting the structure of the rest of the report.
- Source attributions: cite the foundational sources that motivate the framing.

## II. Background and current state (600-800 words)
- Section 2.1 — Where {topic} stands today
  - Definitional clarifications: what is meant by the key terms in the literature.
  - The state of play: market size, policy posture, dominant players.
  - {facts[0]}.
- Section 2.2 — How we got here
  - The recent trajectory and the inflection points that produced it.
  - {facts[1]}.
- Section 2.3 — The frame for the rest of the report
  - The lens through which the subsequent analysis is conducted.
  - Anticipated objections to the framing, and the response.

## III. Core themes from the source material (900-1200 words)
- Theme 1: **{facts[2]}**
  - Lead with the strongest source-level evidence.
  - Cross-source corroboration: which other sources support this finding.
  - Counter-points and remaining uncertainties.
- Theme 2: **{facts[3]}**
  - Present the source-by-source evidence.
  - Flag where the sources differ on degree, even where they agree on direction.
  - {facts[4]}.
- Theme 3: **{facts[5]}**
  - This is the most empirically grounded theme; lead with quantitative anchors.
  - {facts[6]}.
- Theme 4: **{facts[7]}**
  - This is the most contested theme; the prose should reflect that.
  - Present at least two distinct interpretive frames and explain when each applies.

## IV. Supporting evidence and quantitative anchors (500-700 words)
- Numerical anchors: the headline numbers that recur across the sources.
- Methodological notes: how the underlying numbers were produced.
- Reconciling apparent disagreements between sources: when they disagree on
  numbers, why, and what each is actually measuring.
- {facts[8]}.

## V. Cross-source synthesis (500-700 words)
- Where the sources agree.
- Where they disagree.
- Which disagreements are substantive vs. semantic.
- The most important uncertainties for downstream decision-makers.
- {facts[9]}.

## VI. Implications and recommendations (500-600 words)
- For policy makers: three concrete recommendations, ordered by leverage.
- For industry leaders: planning recommendations that don't depend on
  specific policy outcomes.
- For investors: where the asymmetric returns and tail risks live.
- {facts[10]}.

## VII. Conclusion (200-300 words)
- Restatement of the central argument.
- The next 5-10 years: what to watch.
- Key indicators that would shift the conclusions.
- {facts[11]}.

## Appendix: methodology and source notes
- How the sources were selected.
- Each source's strengths and weaknesses.
- The boundary conditions for the conclusions.
- A short note on the cadence at which this analysis should be refreshed.

## Editorial notes
- Target length: 2,500-3,500 words.
- Tone: analytical, source-grounded, direct.
- Avoid hedging language unless the underlying evidence warrants it.
- Cite each source by URL on first mention; thereafter use a short label.
- The recommendations section is the deliverable; the rest is scaffolding.
"""


def _make_write_response(history: list[tuple[str, str]], topic: str) -> str:
    """Long-form report. Targeted at ~5000 tokens of output."""
    user_text = _last_user_text(history)
    seed = _stable_seed(user_text + topic + "write")
    facts = _pick(seed, _FACT_BANK, 14)
    return f"""# Research report: {topic}

## Executive summary

This report synthesises findings on **{topic}** drawn from five primary
sources. Across the literature several themes emerge consistently and merit
highlighting for stakeholders weighing investment, policy, or operational
decisions. The dominant pattern is one of structural transformation: the
underlying technology and economics of {topic} have moved to a qualitatively
new state in the past decade, and the binding constraints on further progress
have shifted from cost to deployment friction. Stakeholders who continue to
model the field on the assumptions of five or ten years ago will systematically
miss both the opportunities and the risks.

The five sources we draw on, while covering different dimensions of {topic} —
technology, policy, economics, market structure, and operations — converge on
a small set of cross-cutting findings. We summarise these as headline findings
and then unpack each in turn, noting where the sources agree, where they
diverge, and what the resulting picture means for practitioners trying to act
on the literature.

## Background

The trajectory of {topic} over the past decade has been shaped by three forces
acting in combination: technological learning curves that have compressed cost
and improved performance at compounding rates; deliberate policy intervention
that has shifted the relative attractiveness of the dominant technology
pathways; and the rebuilding of supply chains around new dominant technologies,
which has both unlocked further cost declines and created new geopolitical
exposures. Each of these forces has produced its own pattern of winners and
losers, and the interaction effects between them are still resolving.

The literature treats this combination of forces as the defining feature of
the current decade. The historical analogue most often cited is the
information-technology revolution of the 1990s and 2000s — a period in which
underlying costs fell at consistent rates and the binding constraints on
adoption shifted from economics to organisational change, regulation, and
infrastructure. The parallel is imperfect, but the structural pattern holds:
durable cost declines coupled with policy choices that determine which
configurations of the technology actually deploy.

## Key findings

1. {facts[0]}.
2. {facts[1]}.
3. {facts[2]}.
4. {facts[3]}.
5. {facts[4]}.

## Detailed analysis

### Finding 1: The cost trajectory

The first observation is that **{facts[0].lower()}**. Multiple sources
corroborate this with quantitative evidence pointing to the same conclusion.
The implication for practitioners is that strategic planning needs to assume
a materially different cost structure than was prevalent five years ago.
Sources note that the trajectory is unlikely to reverse, and that the binding
constraints have shifted from cost to deployment friction.

The most quantitatively grounded source documents the cost trajectory using
multiple complementary metrics — levelised cost, capital cost per unit of
capacity, and operational cost per unit of output. The agreement across these
metrics is itself notable, since each is sensitive to different drivers.
Practitioners working from a five-year-old planning baseline will find that
projects which were previously marginal are now materially attractive, and
that the field of viable competitors has both expanded and shifted in
composition.

### Finding 2: Concentration and fragility

The second observation is that **{facts[1].lower()}**. This concentration
creates both efficiency benefits — economies of scale, learning effects — and
fragility, in that disruptions to the dominant clusters could affect global
supply. Mitigations are being pursued in multiple regions, but are years from
materially shifting the picture.

The literature distinguishes between two kinds of concentration: capacity
concentration (where the production happens) and ownership concentration
(who owns the productive capacity). Both have grown, but they are not the
same phenomenon and they imply different risks. Capacity concentration is a
logistics and resilience concern; ownership concentration is a market-power
concern. Conflating them produces poor policy analysis.

### Finding 3: Policy as the binding variable

The third observation, that **{facts[2].lower()}**, is closely linked to the
fourth: **{facts[3].lower()}**. Together these point to an environment where
political and regulatory factors are at least as decisive as pure technology
or unit-economics considerations. Stakeholders who model {topic} purely on
engineering and cost terms are likely to miss the dominant variables.

The policy environment varies substantially across jurisdictions, and the
divergence is widening rather than narrowing. Decision-makers operating in
multiple jurisdictions need to develop separate planning frames for each, and
should be cautious about transferring playbooks from one regulatory
environment to another.

### Finding 4: Structural shifts in deployment patterns

A further observation worth highlighting is **{facts[5].lower()}**, which
serves as a counterweight to the headline narrative of rapid progress, and a
fifth worth tracking is **{facts[6].lower()}**, with notable implications for
small- and medium-sized players in the value chain. These two findings point
to a more uneven pattern of deployment than the headline cost numbers might
suggest. The aggregate picture remains favourable, but the distribution of
benefits and costs across actors is becoming more skewed.

### Finding 5: Supporting structural observations

Three additional findings round out the picture: **{facts[7].lower()}**,
**{facts[8].lower()}**, and **{facts[9].lower()}**. These do not change the
headline conclusions but they refine the implementation guidance that follows
from them. Practitioners attending to these supporting findings will find
their planning more robust against the kinds of shocks the literature
identifies as plausible over the relevant horizon.

## Cross-source synthesis

The most striking pattern across the five sources is the consistency of the
direction of change paired with substantial disagreement about pace. All
sources agree that the trajectory of {topic} is one of significant
transformation; they differ on timeline, on which actors will lead, and on
which sub-segments will see the steepest changes.

A second cross-cutting theme is the increasing role of **{facts[10].lower()}**
as a structural feature, which several sources note as a recurring topic that
will shape outcomes through the next decade. The mechanism by which this
feature operates is contested across the sources but its presence is not.

Sources differ most on the question of which mitigation strategies will scale.
Two of the five lean toward optimism on technology breakthroughs unlocking
new phases of deployment; the others place more weight on policy and on
infrastructure-side execution as the rate-limiting factors. The reality is
likely a mix of all four, with the relative weights evolving year over year.
Pinning planning assumptions to any single one of these scenarios is
ill-advised; building optionality across them is the more defensible move.

A third cross-cutting theme worth noting is **{facts[11].lower()}**. This
shows up across the sources as an undercurrent rather than a headline finding,
but its consequences could be substantial if any of the contingent
preconditions actually materialise.

## Sources cited

This synthesis draws on five primary sources covering technology, policy,
economics, market structure, and operational considerations relevant to
{topic}. References are available in the workspace under `sources/`. We have
treated each source as a distinct point of view; where their findings converge
we have flagged that explicitly, and where they diverge we have presented
both views and indicated which we find more persuasive.

The methodological notes for each source — how the underlying numbers were
produced, what the boundary conditions are, where the framing has been
contested — are summarised in an appendix that should accompany this report.

## Recommendations

For decision-makers, the practical recommendations are:

1. **Track cost-curve indicators on a quarterly cadence**, not an annual one.
   The pace of change makes annual planning cycles too coarse to capture
   inflections that materially affect investment economics.
2. **Build optionality into supply-chain strategy**, given the geopolitical
   concentration risks identified across multiple sources. The cost of
   maintaining redundant qualified suppliers is low relative to the
   protection it provides against the tail-risk scenarios.
3. **Maintain active engagement with policy developments** at both subnational
   and national levels. The literature is consistent that policy is the
   binding variable; treating it as a passive input to planning is a
   strategic error.
4. **Revisit underlying assumptions at regular intervals**, given the unusual
   rate of change in the underlying technologies and policies. Plans built
   on assumptions that were valid two years ago will increasingly fail to
   reflect the operating environment.

A specific recommendation for resource-constrained organisations: prioritise
investments in **{facts[12].lower()}** and in **{facts[13].lower()}**, which
the literature consistently identifies as cross-cutting enablers regardless
of which technology pathway dominates. Spending on these is robust to the
uncertainty about which specific scenarios materialise.

Confidence in these conclusions is moderate-to-high; the most uncertain
elements relate to political durability of current policy commitments and
the realised cost trajectories of pre-commercial technology pathways. Both
of these uncertainties cut in both directions and should be treated as
genuine planning unknowns rather than as latent biases in any one direction.

## Conclusion

The trajectory of {topic} over the next 5-10 years is overwhelmingly likely
to involve continued transformation along the dimensions the five sources
have identified. The pace and the distribution of effects across actors are
the genuine open questions; the direction is not. Practitioners who treat
the headline findings as planning baselines and the policy commentary as
scenario inputs will be using the literature as it was intended, and will
position themselves to act on the opportunities the next phase will create.
"""


def _make_polish_response(history: list[tuple[str, str]], topic: str) -> str:
    """Polished / finalised version. Targeted at ~3000 tokens of output."""
    user_text = _last_user_text(history)
    seed = _stable_seed(user_text + topic + "polish")
    facts = _pick(seed, _FACT_BANK, 10)
    return f"""# {topic.title()} — Final Report

## Executive summary

This polished synthesis distils five primary sources on **{topic}** into a
concise brief for decision-makers. The analytical thrust is consistent across
sources even where pace and pathway projections diverge: the trajectory of
{topic} is one of structural transformation, the binding constraints have
shifted from cost to deployment friction, and the policy environment is the
dominant variable in most jurisdictions.

The brief that follows is organised around five headline findings, three
cross-source themes, and four concrete recommendations for decision-makers.
Confidence is high on the empirical findings and moderate on the
forward-looking projections; the explicit caveats around political durability
and pre-commercial technology pathways are flagged in context.

## Headline findings

1. **{facts[0]}.** This is the dominant empirical finding across the sources;
   it is also the finding with the strongest quantitative grounding.
2. **{facts[1]}.** A structural feature with both efficiency benefits and
   tail-risk implications; the latter increasingly drives strategic planning.
3. **{facts[2]}.** The binding variable in most jurisdictions; treating it as
   a passive input to planning is a strategic error the literature flags
   repeatedly.
4. **{facts[3]}.** Closely linked to the prior finding and reinforcing the
   conclusion that political and regulatory factors are at least as decisive
   as pure technology or unit-economics considerations.
5. **{facts[4]}.** A second-order finding that nonetheless matters for
   anyone trying to translate the headline conclusions into operational
   decisions.

## Why this matters

The implications for {topic} are concrete. Decision-makers should treat the
five findings above as planning baselines rather than as scenarios. The
cost-curve trajectory is durable; the regulatory environment is the binding
variable in most jurisdictions; supply chain concentration is a sustained
tail risk; and the operational implications cascade through procurement,
hiring, capital planning, and stakeholder management.

The framing matters. Too often, organisations build plans on assumptions that
were valid two or three years ago and discover the disconnect only when
adverse events crystallise. The literature on {topic} is moving fast enough
that planning frameworks should be refreshed at least annually, with the most
sensitive components reviewed quarterly.

## Cross-source view

All five sources agree on the direction of change but differ on the pace and
on which actors will lead. The recurring undertone across the literature is
**{facts[5].lower()}**, which multiple sources flag as a structural feature
shaping the next decade.

A second recurring theme is **{facts[6].lower()}**, which several sources
note as a sustained pattern with implications for small- and medium-sized
participants in the value chain. The headline cost numbers are favourable in
aggregate but the distribution of benefits is becoming more skewed.

A third theme that emerges across the sources is **{facts[7].lower()}**. This
shows up as an undercurrent rather than a headline conclusion, but the
consequences could be substantial if any of the contingent preconditions
actually materialise. Practitioners should track this theme even though no
single source elevates it to a top finding.

## Disagreements and uncertainties

The sources diverge most on the pace of transition and on the relative weight
of technology breakthroughs vs. policy execution as rate-limiting factors.
Two of the five lean toward optimism on technology breakthroughs; the other
three place more weight on infrastructure and policy execution. The reality
is likely a mix, with the relative weights evolving year over year.

A second axis of disagreement is the role of **{facts[8].lower()}**. The
literature is split between treating this as a transient phenomenon that will
resolve as the field matures and treating it as a durable feature that
practitioners need to design around. The evidence is closer to balanced than
either side acknowledges; planning frameworks should accommodate both
interpretations.

The most important uncertainty for decision-makers is the durability of
current policy commitments. Both major jurisdictions reviewed in the
literature have made multi-year commitments that materially shift the
economics; both face political pressures that could erode those commitments.
Plans should be built to remain robust if any single commitment is
materially weakened.

## Final recommendations

1. **Track cost-curve indicators on a quarterly cadence.** Annual planning
   cycles are too coarse for the current pace of change.
2. **Build supply-chain optionality given concentration risks.** The cost of
   maintaining redundant qualified suppliers is low relative to the
   protection it provides against tail-risk scenarios.
3. **Maintain active engagement with policy developments** at both
   subnational and national levels.
4. **Revisit assumptions at regular intervals.** Plans built on
   two-year-old assumptions will increasingly fail to reflect the
   operating environment.

For organisations with limited resources, the highest-leverage allocations
are toward **{facts[9].lower()}**, which the literature consistently
identifies as a cross-cutting enabler regardless of which technology
pathway dominates. Investing here is robust to the scenario uncertainty.

## Closing note

The source material supports these recommendations with high confidence.
Sensitivity to political factors is explicitly flagged in the body. The
recommended cadence for refreshing this analysis is six to twelve months,
or sooner if a discontinuous policy or technology event occurs.

---

**Status:** Final. Ready for distribution to a decision-maker audience.

**Cadence for refresh:** Six to twelve months under normal conditions, or
within thirty days of any discontinuous event in the operating environment.
"""


def _make_short_response(history: list[tuple[str, str]]) -> str:
    return "Acknowledged. Proceeding."


def call_mock_llm(
    history: list[tuple[str, str]],
    *,
    purpose: str = "extraction",
    topic: str = "",
) -> str:
    """Return a deterministic LLM-style response shaped by ``purpose``."""
    if purpose == "outline":
        return _make_outline_response(history, topic)
    if purpose == "write":
        return _make_write_response(history, topic)
    if purpose == "polish":
        return _make_polish_response(history, topic)
    if purpose == "short":
        return _make_short_response(history)
    return _make_extraction_response(history)


# ---------------------------------------------------------------------------
# Service availability probes
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
# Run accounting
# ---------------------------------------------------------------------------


@dataclass
class LLMCallRecord:
    """One LLM invocation, with token counts."""

    step: str
    prompt_tokens: int
    response_tokens: int
    duration_ms: int = 0


@dataclass
class StepRecord:
    """Wallclock + token rollup per workflow step."""

    name: str
    started_at: float
    finished_at: float
    snapshot_id: str | None
    skipped: bool = False
    skipped_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class RunRecord:
    """Bookkeeping for a single ``workflow_agent.py`` invocation."""

    topic: str
    workspace_name: str
    workflow_id: str
    crash_at: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    crashed: bool = False
    completed: bool = False
    backend: str = "simulated"  # "sdk" or "simulated"
    llm_calls: list[LLMCallRecord] = field(default_factory=list)
    steps: list[StepRecord] = field(default_factory=list)

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

    @property
    def wall_clock_seconds(self) -> float:
        if self.finished_at is None:
            return 0.0
        return self.finished_at - self.started_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "workspace_name": self.workspace_name,
            "workflow_id": self.workflow_id,
            "crash_at": self.crash_at,
            "crashed": self.crashed,
            "completed": self.completed,
            "backend": self.backend,
            "wall_clock_seconds": self.wall_clock_seconds,
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
            "steps": [
                {
                    "name": s.name,
                    "duration_ms": int((s.finished_at - s.started_at) * 1000),
                    "snapshot_id": s.snapshot_id,
                    "skipped": s.skipped,
                    "skipped_reason": s.skipped_reason,
                    "input_tokens": s.input_tokens,
                    "output_tokens": s.output_tokens,
                }
                for s in self.steps
            ],
        }


def history_to_prompt(history: list[tuple[str, str]]) -> str:
    """Flatten chat history into one text blob for tokenisation."""
    return "\n".join(f"<{role}>\n{content}\n</{role}>" for role, content in history)


def llm_call(
    history: list[tuple[str, str]],
    *,
    step: str,
    purpose: str,
    topic: str,
    record: RunRecord,
) -> str:
    """Call the mock LLM, record token counts, return the response text."""
    prompt = history_to_prompt(history)
    prompt_tokens = count_tokens(prompt)
    start = time.perf_counter()
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
# Simulated workspace + workflow stores (file-backed, durable across procs)
# ---------------------------------------------------------------------------


class SimulatedWorkspaceStore:
    """File-backed workspace simulating ``ws.kv``, ``ws.files``, ``ws.snapshot``."""

    def __init__(self, name: str) -> None:
        self.name = name
        SIM_DATA_DIR.mkdir(parents=True, exist_ok=True)
        slug = slugify(name)
        self._path = SIM_DATA_DIR / f"workspace-{slug}.json"
        self._state = self._load()
        self.id = self._state.setdefault("id", f"ws_{short_ulid()}")
        if "kv" not in self._state:
            self._state["kv"] = {}
        if "files" not in self._state:
            self._state["files"] = {}
        if "snapshots" not in self._state:
            self._state["snapshots"] = []
        self._save()

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"kv": {}, "files": {}, "snapshots": []}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return {"kv": {}, "files": {}, "snapshots": []}

    def _save(self) -> None:
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    @property
    def kv(self) -> _SimulatedKV:
        return _SimulatedKV(self)

    @property
    def files(self) -> _SimulatedFiles:
        return _SimulatedFiles(self)

    def snapshot(self, name: str, *, message: str | None = None) -> _SimulatedSnapshot:
        snap_id = f"snap_{short_ulid()}"
        snap = {
            "id": snap_id,
            "name": name,
            "message": message,
            "kv_keys": sorted(self._state["kv"].keys()),
            "file_paths": sorted(self._state["files"].keys()),
            "created_at": time.time(),
        }
        self._state["snapshots"].append(snap)
        self._save()
        return _SimulatedSnapshot(snap_id, name)


@dataclass
class _SimulatedSnapshot:
    """Lightweight snapshot view returned by :meth:`SimulatedWorkspaceStore.snapshot`."""

    id: str
    name: str


class _SimulatedKV:
    """KV proxy backed by a :class:`SimulatedWorkspaceStore`."""

    _MISSING = object()

    def __init__(self, store: SimulatedWorkspaceStore) -> None:
        self._store = store

    def set(self, key: str, value: Any) -> None:
        self._store._state["kv"][key] = value
        self._store._save()

    def get(self, key: str, *, default: Any = _MISSING) -> Any:
        if key in self._store._state["kv"]:
            return self._store._state["kv"][key]
        if default is _SimulatedKV._MISSING:
            raise KeyError(key)
        return default


class _SimulatedFiles:
    """Files proxy backed by a :class:`SimulatedWorkspaceStore`."""

    _MISSING = object()

    def __init__(self, store: SimulatedWorkspaceStore) -> None:
        self._store = store

    def write(self, path: str, content: str) -> None:
        self._store._state["files"][path] = content
        self._store._save()

    def read(self, path: str, *, default: Any = _MISSING, as_text: bool = True) -> str:
        if path in self._store._state["files"]:
            return self._store._state["files"][path]
        if default is _SimulatedFiles._MISSING:
            raise KeyError(path)
        return default

    def exists(self, path: str) -> bool:
        return path in self._store._state["files"]


@dataclass
class StepInfo:
    """Single step entry in a simulated workflow."""

    id: str
    name: str
    status: str
    started_at: float | None = None
    finished_at: float | None = None
    input: Any = None
    output: Any = None
    error: str | None = None
    snapshot_id: str | None = None
    attempt: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "input": self.input,
            "output": self.output,
            "error": self.error,
            "snapshot_id": self.snapshot_id,
            "attempt": self.attempt,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StepInfo:
        return cls(**data)


@dataclass
class ResumeInfo:
    """Resumption snapshot: where to pick up the workflow."""

    workflow_id: str
    workflow_status: str
    next_step: str | None
    last_completed: StepInfo | None
    snapshot_id: str | None


class SimulatedWorkflow:
    """One workflow's state, as persisted to disk.

    Mirrors the contract from CONTRACTS.md — manifest of expected
    steps + append-only step log + a derived ``status`` and
    ``resume_info``.
    """

    def __init__(self, store: SimulatedWorkflowStore, data: dict[str, Any]) -> None:
        self._store = store
        self.id: str = data["id"]
        self.name: str = data["name"]
        self.steps_manifest: list[str] = list(data.get("steps_manifest") or [])
        self.steps: list[StepInfo] = [
            StepInfo.from_dict(s) for s in data.get("steps", [])
        ]
        self.created_at: float = data.get("created_at") or time.time()
        self.started_at: float | None = data.get("started_at")
        self.finished_at: float | None = data.get("finished_at")
        self._status: str = data.get("status") or "pending"
        self.metadata: dict[str, Any] = dict(data.get("metadata") or {})

    @property
    def status(self) -> str:
        return self._status

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "steps_manifest": list(self.steps_manifest),
            "steps": [s.to_dict() for s in self.steps],
            "status": self._status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "metadata": self.metadata,
        }

    def start_step(
        self,
        name: str,
        *,
        input: Any = None,  # noqa: A002 - mirrors v0.2 SDK surface
    ) -> StepInfo:
        if name not in self.steps_manifest:
            raise ValueError(
                f"Step {name!r} not in workflow manifest {self.steps_manifest!r}"
            )
        attempt = 1 + sum(1 for s in self.steps if s.name == name)
        step = StepInfo(
            id=f"step_{short_ulid()}",
            name=name,
            status="running",
            started_at=time.time(),
            input=input,
            attempt=attempt,
        )
        self.steps.append(step)
        if self.started_at is None:
            self.started_at = step.started_at
        self._status = "running"
        self._store._save()
        return step

    def complete_step(
        self,
        step_id: str,
        *,
        output: Any = None,
        snapshot_id: str | None = None,
    ) -> StepInfo:
        step = self._get_step(step_id)
        step.status = "completed"
        step.finished_at = time.time()
        step.output = output
        step.snapshot_id = snapshot_id
        completed_names = {s.name for s in self.steps if s.status == "completed"}
        if all(name in completed_names for name in self.steps_manifest):
            self._status = "completed"
            self.finished_at = step.finished_at
        else:
            self._status = "running"
        self._store._save()
        return step

    def fail_step(self, step_id: str, *, error: str) -> StepInfo:
        step = self._get_step(step_id)
        step.status = "failed"
        step.finished_at = time.time()
        step.error = error
        self._store._save()
        return step

    def _get_step(self, step_id: str) -> StepInfo:
        for s in self.steps:
            if s.id == step_id:
                return s
        raise KeyError(step_id)

    def resume_info(self) -> ResumeInfo:
        completed_names = {s.name for s in self.steps if s.status == "completed"}
        next_step: str | None = None
        for name in self.steps_manifest:
            if name not in completed_names:
                next_step = name
                break

        last_completed: StepInfo | None = None
        for s in reversed(self.steps):
            if s.status == "completed":
                last_completed = s
                break

        return ResumeInfo(
            workflow_id=self.id,
            workflow_status=self._status if next_step else "completed",
            next_step=next_step,
            last_completed=last_completed,
            snapshot_id=last_completed.snapshot_id if last_completed else None,
        )


class SimulatedWorkflowStore:
    """Disk-backed registry of simulated workflows for one workspace."""

    def __init__(self, workspace_name: str) -> None:
        SIM_DATA_DIR.mkdir(parents=True, exist_ok=True)
        slug = slugify(workspace_name)
        self._path = SIM_DATA_DIR / f"workflows-{slug}.json"
        self._workflows: dict[str, SimulatedWorkflow] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return
        for wf_id, wf_data in (data.get("workflows") or {}).items():
            self._workflows[wf_id] = SimulatedWorkflow(self, wf_data)

    def _save(self) -> None:
        tmp = self._path.with_suffix(".json.tmp")
        payload = {
            "workflows": {wf_id: wf.to_dict() for wf_id, wf in self._workflows.items()},
        }
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    def get_or_create(
        self,
        name: str,
        *,
        steps: list[str],
        metadata: dict[str, Any] | None = None,
    ) -> SimulatedWorkflow:
        # Idempotent on (workspace_name, name): returns the existing one
        # if it exists, else creates a new one.
        for wf in self._workflows.values():
            if wf.name == name:
                return wf
        wf = SimulatedWorkflow(
            self,
            {
                "id": f"wf_{short_ulid()}",
                "name": name,
                "steps_manifest": list(steps),
                "steps": [],
                "status": "pending",
                "created_at": time.time(),
                "metadata": metadata or {},
            },
        )
        self._workflows[wf.id] = wf
        self._save()
        return wf

    def get(self, workflow_id: str) -> SimulatedWorkflow:
        if workflow_id not in self._workflows:
            raise KeyError(workflow_id)
        return self._workflows[workflow_id]

    def list(self) -> list[SimulatedWorkflow]:
        return list(self._workflows.values())


__all__ = [
    "GATEWAY_URL",
    "LLMCallRecord",
    "MOCK_MCP_URL",
    "ResumeInfo",
    "RunRecord",
    "SIM_DATA_DIR",
    "SimulatedWorkflow",
    "SimulatedWorkflowStore",
    "SimulatedWorkspaceStore",
    "StepInfo",
    "StepRecord",
    "WORKSPACE_URL",
    "call_mock_llm",
    "count_tokens",
    "estimate_cost",
    "get_fixture_sources",
    "history_to_prompt",
    "llm_call",
    "reset_simulation_state",
    "services_available",
    "short_ulid",
    "slugify",
]
