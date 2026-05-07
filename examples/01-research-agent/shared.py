# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Shared building blocks for the research-agent demo.

Both ``baseline.py`` and ``with_plinth.py`` import from this module so
that the only meaningful difference between them is *how they hold
state* and *how they call tools* — not the fixtures, not the prompts,
not the token counter.

Why this design:
- The headline number ("X% fewer tokens with Plinth") only matters if
  the comparison is apples-to-apples. We get that by sharing the LLM
  mock, the sources, the extraction prompts, and the synthesis prompt.
- Simulation mode is fully self-contained: the fixtures live in this
  file. The demo runs from a fresh clone with zero infrastructure.
- Live mode (``--mode=live``) reuses the same fixtures as ``mock://``
  URLs, so the only thing that hits the network is the Anthropic LLM
  call itself. That keeps the cost knobs predictable for users running
  the live mode against their own API key.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
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
# Service endpoints (only consulted in non-simulation modes)
# ---------------------------------------------------------------------------

WORKSPACE_URL = os.environ.get("PLINTH_WORKSPACE_URL", "http://localhost:7421")
GATEWAY_URL = os.environ.get("PLINTH_GATEWAY_URL", "http://localhost:7422")
MOCK_MCP_URL = os.environ.get("PLINTH_MOCK_MCP_URL", "http://localhost:7423")


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
# Fixture sources — bundled for offline simulation mode
# ---------------------------------------------------------------------------
#
# Each topic has 5 sources, each ~1400-1700 words (~1800-2200 tokens). This
# sizing matters for the headline result: in baseline, all five sources end
# up inlined in the synthesis-step prompt, which is where the cost balloons.

_RENEWABLE_SOURCES: list[dict[str, str]] = [
    {
        "url": "mock://renewable-energy-1",
        "title": "Solar Power: From Niche to Mainstream Energy Source",
        "snippet": "How photovoltaic technology became the cheapest electricity source in history.",
        "content": """Solar power has undergone one of the most remarkable cost transformations in the history of any industrial technology. Between 2010 and 2025, the levelized cost of electricity from utility-scale photovoltaic installations fell by approximately 89 percent, dropping from around 28 cents per kilowatt-hour to under 4 cents in the most favorable markets. This decline was not the product of a single technological breakthrough but rather the cumulative effect of incremental improvements compounding through global manufacturing scale.

The dominant technology, crystalline silicon solar cells, has seen efficiency gains from roughly 14 percent module efficiency in 2010 to 22-24 percent for the best commercial modules in 2025. More importantly, manufacturing scale in China grew the global module production capacity from 25 gigawatts per year to over 600 gigawatts per year over the same period. The resulting overcapacity drove relentless price competition, which in turn forced producers to pursue further automation and bill-of-materials reductions, creating a positive feedback loop that economists call a learning curve.

Utility-scale solar farms now span tens of thousands of acres in regions like the American Southwest, Western Australia, India's Rajasthan, and the Atacama Desert in Chile. The largest single-site installation as of 2025 exceeds 5 gigawatts of generating capacity, larger than most nuclear power stations. These facilities employ tracking systems that follow the sun's path through the sky, increasing daily generation by 15 to 25 percent compared to fixed-tilt arrays.

Distributed generation — rooftop systems on homes, commercial buildings, and warehouses — represents a parallel revolution. In Australia, more than one in three single-family homes has rooftop solar. In Germany, the figure is approaching one in four. In California, new homes built since 2020 must include solar capacity by law. The economics of self-consumption have fundamentally restructured electricity bills for these households, with payback periods now under 6 years in most markets.

The intermittency problem — solar produces nothing at night and less in winter — is increasingly addressed through battery storage. Lithium iron phosphate cells fell 85 percent in cost over the same 15-year window. Utilities now routinely install solar paired with 4-hour battery systems, providing dispatchable evening peak power. The next frontier is multi-day storage, addressed through novel chemistries like iron-air batteries and through long-distance transmission that pools generation across continental areas.

Critics point to several real challenges that remain. Solar manufacturing requires significant energy and produces meaningful upstream emissions; the energy payback period is roughly 1 to 3 years depending on installation site. End-of-life recycling for photovoltaic panels remains underdeveloped, with most decommissioned panels still entering landfills as of 2024. Land use for utility-scale projects can conflict with agricultural and conservation priorities, although agrivoltaics — co-locating crops or grazing with solar arrays — is showing promise as a dual-use solution.

Materials supply chains present geopolitical concerns. Polysilicon production is concentrated in a handful of Chinese provinces; silver demand from cell manufacturing has been one of the largest sources of marginal silver demand globally. Trade policy responses, including U.S. and EU tariffs on Chinese modules, have shifted some manufacturing to Southeast Asia and India but have not displaced China as the dominant supplier.

Looking forward, perovskite tandem cells promise efficiency gains beyond the silicon ceiling, with laboratory cells now exceeding 33 percent. Commercial deployment timelines are uncertain due to durability questions, but several companies are bringing first products to market in 2025-2026. Integration of solar into building materials — solar shingles, solar windows, vehicle-integrated photovoltaics — represents another growth vector. The International Energy Agency projects that solar will become the world's largest single source of electricity generation before 2030, surpassing coal.

The policy landscape that enabled this transition combined direct subsidies, feed-in tariffs in early stages, mandates for renewable portfolio standards, accelerated permitting for small-scale installations, and most recently in the United States, the Inflation Reduction Act's production tax credits. The lesson from solar's trajectory — that targeted policy interventions can compound with manufacturing learning curves to produce decadal cost declines of an order of magnitude — is now being deliberately applied to other clean technologies including wind, batteries, and electrolyzers for hydrogen production."""
    },
    {
        "url": "mock://renewable-energy-2",
        "title": "Wind Power: Onshore, Offshore, and the Path to Terawatt-Scale Deployment",
        "snippet": "Modern wind turbines, grid integration, and the economics of offshore wind farms.",
        "content": """Wind energy has scaled to over 1 terawatt of installed global capacity by 2025, generating roughly 9 percent of world electricity. The trajectory mirrors solar in its cost decline but has progressed through different technology levers. While solar improved through cell-level efficiency and module-level manufacturing scale, wind progressed primarily through making turbines bigger.

The modal onshore turbine in 2010 had a rotor diameter around 80 meters and a hub height around 80 meters, with a rated capacity of 2 to 3 megawatts. By 2025, the mainstream onshore turbine has a rotor diameter of 150 to 170 meters, hub heights of 130 to 160 meters, and rated capacities of 5 to 7 megawatts per unit. This is roughly an order of magnitude more energy harvest per turbine, achieved primarily by sweeping a larger area of the wind resource at higher altitudes where wind is steadier and faster.

Offshore wind has gone through an even more dramatic scale-up. The first commercial offshore wind farm — Vindeby in Denmark, commissioned in 1991 — used 11 turbines of 450 kilowatts each, a total project size of about 5 megawatts. The current state of the art for fixed-bottom offshore turbines is around 18 megawatts per unit, with rotor diameters approaching 250 meters. Project sizes routinely exceed 1 gigawatt; the largest operational projects in the North Sea exceed 3 gigawatts.

Floating offshore wind opens up vast deepwater resources that fixed-bottom installations cannot reach. Pilot projects off Scotland, Portugal, and California have demonstrated technical viability. The first commercial-scale floating projects, in the 100-300 megawatt range, are being commissioned in 2025-2026. The economics of floating wind remain less competitive than fixed-bottom but are following a learning curve similar to early offshore.

Grid integration has emerged as the dominant constraint on continued wind deployment in mature markets. The fundamental challenge: wind resource is geographically concentrated, often far from population centers, while electricity grids were designed around centralized fossil generation near load. Texas, Iowa, and the central United States produce far more wind power than they can locally consume, and transmission limits the ability to move that power to demand centers on the coasts.

Curtailment — being paid to not generate when local demand is saturated and transmission is full — has become a significant economic factor in wind-rich regions. Some Texas wind farms are curtailed for several hundred hours per year. The mitigation is more transmission, a notoriously difficult permitting and political problem in the United States and Europe alike.

Repowering — replacing aging turbines with modern, larger units on existing sites — has become a meaningful share of new capacity additions. A site permitted in 2005 with twenty 1.5 MW turbines might be repowered with seven 5 MW turbines, more than doubling generation while reducing the number of moving parts. The land-use efficiency improvement is substantial.

Wildlife impacts, particularly bird and bat mortality, remain a real but addressable concern. Population-level effects on most species are well below other anthropogenic mortality sources like buildings, vehicles, and free-roaming cats. Site-specific siting protocols, radar-triggered curtailment during migration periods, and ultrasonic deterrent systems for bats are reducing mortality at well-managed sites. Eagle mortality in particular receives substantial regulatory attention in the United States.

Wind manufacturing is more geographically distributed than solar. Vestas (Denmark), Siemens-Gamesa (Spain/Germany), and General Electric (United States) compete with Goldwind, Mingyang, and Envision (China). Component supply chains span dozens of countries; large blade manufacturing in particular is logistically sensitive due to the difficulty of shipping 80-meter blades, which has led to localized blade factories near major project regions.

The next decade is expected to see wind capacity at least double from 2025 levels, with offshore growing several-fold and floating offshore reaching first commercial-scale deployment. Constraints will continue to shift from technology to permitting, transmission, and supply chain bottlenecks rather than fundamental cost. The U.S. Inflation Reduction Act and European REPowerEU plan have committed substantial subsidies through the early 2030s; the question is whether the supply chain and permitting timelines can keep pace."""
    },
    {
        "url": "mock://renewable-energy-3",
        "title": "Battery Storage and the Reshaping of Electricity Markets",
        "snippet": "How lithium-ion and emerging chemistries are enabling 100% renewable grids.",
        "content": """Grid-scale battery storage has gone from a niche pilot technology in 2015 to one of the largest sources of new generating capacity additions in the United States by 2024. The driver is straightforward: lithium-ion battery cell prices fell from around $1100 per kilowatt-hour in 2010 to under $90 per kilowatt-hour by 2024, a roughly 92 percent decline. The trajectory mirrors solar's cost curve but compressed into half the time.

The dominant chemistry for stationary applications is now lithium iron phosphate, or LFP. LFP cells trade some energy density compared to nickel-rich chemistries for substantially better cycle life, thermal stability, and lower raw material cost. For stationary storage, where weight and volume are less constrained than in vehicles, the trade is favorable. By 2025, more than 70 percent of new utility-scale battery deployments use LFP cells.

The most common configuration for utility-scale projects is a 4-hour duration system: a 100 megawatt power rating with 400 megawatt-hours of energy storage. This sizing reflects the typical evening peak load profile in solar-rich grids, where solar production drops at sunset just as residential air-conditioning and lighting demand peaks. Discharging stored solar energy across that 3-5 hour window has emerged as a high-value market.

California's grid is the canonical case study. The state grew from approximately 250 megawatts of battery storage in 2020 to over 13 gigawatts by 2024. On many summer evenings, batteries provide more power to the California grid than natural gas does — a transition that observers in 2018 considered to be at least a decade away.

Beyond 4-hour duration, the economics shift significantly. The capital cost of a battery scales roughly linearly with energy storage capacity, while the value of additional duration falls off quickly past the typical 4-hour evening peak. This has motivated research into longer-duration storage technologies that can store energy more cheaply at the cost of lower power efficiency.

Emerging chemistries include iron-air batteries, which store energy through reversible oxidation of metallic iron. Form Energy, the leading commercial developer, has begun deploying 100-hour duration systems at electric utility sites in 2024-2025. The energy capital cost is substantially lower than lithium-ion but the round-trip efficiency is also lower, around 50 percent compared to 90 percent for lithium. This trade is favorable for systems that cycle infrequently — a few times per year, riding out multi-day weather events that produce sustained low solar and wind output.

Other long-duration approaches under commercial development include flow batteries (vanadium and zinc-bromine), thermal storage (molten salt, hot rocks, hot bricks), gravitational storage (pumped hydro and novel weight-based systems), and compressed-air storage (in salt caverns or man-made vessels). Each has different cost-duration trade-offs, and the market is unlikely to converge on a single winning chemistry for all applications.

Pumped hydro storage remains the largest installed capacity globally, with over 180 gigawatts of installed capacity and durations ranging from 6 hours to several days. New pumped hydro projects are constrained by site availability and permitting, but several large projects are advancing in Australia, China, and the western United States.

A second-order effect of cheap batteries is the restructuring of wholesale electricity markets. Time-of-day price spreads, historically driven by the daily fossil fuel cost stack, are now increasingly driven by battery arbitrage. As batteries proliferate, the spread compresses, eroding the very arbitrage opportunity that paid for the battery investments. This dynamic is creating challenging investment cases for new entrants in saturated markets like California.

Battery raw material supply chains remain a strategic concern. Lithium, cobalt, and nickel mining are concentrated in a handful of countries, with significant reserves in geopolitically sensitive jurisdictions. The shift to LFP has substantially reduced cobalt and nickel demand for stationary storage, while lithium remains the critical bottleneck. Lithium prices were highly volatile through the 2020-2024 period, ranging from below $7,000 per ton to over $80,000 per ton, before settling in the $10,000-15,000 range in 2025 as new mining capacity came online.

Recycling of lithium-ion batteries is scaling alongside deployment, driven by both economic recovery of valuable cathode materials and regulatory mandates in the EU and California. Recovery rates of over 95 percent for nickel and cobalt and over 80 percent for lithium are now achievable in commercial recycling operations. As the first wave of grid batteries reaches end of life in the 2030s, secondary supply from recycling is expected to meaningfully reduce primary mining requirements."""
    },
    {
        "url": "mock://renewable-energy-4",
        "title": "Green Hydrogen: Promise, Progress, and the Cost Gap",
        "snippet": "The role of electrolysis-derived hydrogen in decarbonizing industry and heavy transport.",
        "content": """Hydrogen produced by electrolyzing water using renewable electricity — green hydrogen — is widely considered essential for decarbonizing parts of the economy that cannot be electrified directly: heavy industry like steelmaking, ammonia production for fertilizers, long-haul shipping, and aviation. As of 2025, however, green hydrogen remains expensive relative to fossil-derived alternatives and project deployment has lagged announcements.

The current global production of hydrogen is approximately 95 million tons per year, used primarily for ammonia synthesis and oil refining. Almost all of this hydrogen is produced from natural gas via steam methane reforming, a process that emits roughly 9 to 10 tons of carbon dioxide per ton of hydrogen produced. Replacing this gray hydrogen with low-carbon alternatives would eliminate roughly 900 million tons of CO2 per year, comparable to the total emissions of Germany.

Electrolyzer technology has two main commercial pathways. Alkaline electrolyzers are the mature workhorse, using potassium hydroxide solution, with decades of commercial track record. Proton exchange membrane, or PEM, electrolyzers are newer, more compact, and more flexible in their response to variable input power, but use platinum-group metal catalysts and cost more per kilowatt of capacity. A third technology, solid oxide electrolyzers, operates at high temperature with potentially higher efficiency but is still in early commercial deployment.

The cost of green hydrogen is dominated by two factors: the levelized cost of the input electricity, and the capital cost of the electrolyzer itself running at a low capacity factor. To produce hydrogen at $2 per kilogram, roughly competitive with steam methane reforming in many markets, electricity must cost around $20 per megawatt-hour and the electrolyzer must be deployed at a capacity factor approaching 50 percent. As of 2025, both conditions are difficult to meet simultaneously: cheap renewable electricity tends to be available in regions where demand for hydrogen is sparse, and pairing electrolyzers with intermittent renewables drives capacity factors well below 50 percent.

The Inflation Reduction Act in the United States introduced production tax credits of up to $3 per kilogram for hydrogen produced with very low carbon intensity, which fundamentally changes the economics of U.S. green hydrogen for the next decade. Final rules issued in 2024 imposed strict requirements on the hourly matching of renewable electricity to electrolyzer operation, additionality of new renewable capacity, and geographic deliverability. These rules, intended to ensure that the hydrogen is genuinely low-carbon, also raise the cost of qualifying.

The European Union's REPowerEU plan and Germany's national hydrogen strategy include similar large subsidy commitments through the early 2030s. The combined U.S. and European public commitment to hydrogen subsidies exceeds $200 billion through 2032.

Deployment to date has been slower than announcements suggested. Of the more than 1,500 hydrogen projects publicly announced globally as of 2024, only a small fraction had reached final investment decision and far fewer were under construction. The gap reflects the difficulty of securing offtake agreements at prices that cover production costs even with subsidy. Industrial buyers have generally been unwilling to commit to long-term offtake at $5-7 per kilogram, the typical real cost of green hydrogen in 2024-2025, when their existing gray hydrogen costs $1-2 per kilogram.

Steelmaking is one of the highest-leverage applications for green hydrogen. Direct-reduction of iron ore using hydrogen, in place of coal, produces sponge iron that can be melted in electric arc furnaces using renewable electricity. Several full-scale projects are advancing in Sweden, Germany, and Spain. Hydrogen-based steelmaking emits roughly 80-90 percent less CO2 than coal-based blast furnace steelmaking. The first commercial green steel was sold in 2021 at a substantial premium; volumes are growing but remain a small fraction of global steel production.

Ammonia for fertilizer is another major target application. Approximately half of all hydrogen demand globally is for ammonia synthesis. Several large green ammonia projects are under construction in regions with exceptional renewable resources — Australia's northwest, Saudi Arabia's NEOM, Chile's Magallanes — with the intent to export ammonia by ship to industrial markets in Europe and East Asia. The first commercial cargoes of green ammonia are expected in 2026-2028.

Long-distance shipping presents a particularly attractive use case for hydrogen-derived fuels because direct electrification of ocean-going vessels is impractical. Methanol synthesized from green hydrogen and captured carbon dioxide is the leading candidate fuel; major shipping lines have ordered tens of methanol-capable vessels for delivery in 2025-2028. The fuel is more expensive than current bunker fuel but the cost passes through to relatively price-insensitive cargo owners.

The skeptical view holds that hydrogen will end up confined to a narrower set of applications than its proponents claim — primarily ammonia, steel, and certain niche industrial uses — and that more ambitious roles in residential heating, light-duty transport, and grid power generation will be displaced by direct electrification. The more optimistic view holds that continued cost declines, paired with the existing global gas infrastructure that can be partially repurposed, will allow hydrogen to play a much larger role across the energy system. Both scenarios remain in play as of 2025."""
    },
    {
        "url": "mock://renewable-energy-5",
        "title": "Grid Modernization: Transmission, Smart Inverters, and the Software Layer",
        "snippet": "How the electricity grid must change to handle high renewable penetration.",
        "content": """The electricity grid was designed around centralized fossil-fuel and hydroelectric generation, with power flowing one direction from large plants to dispersed consumers. High penetrations of variable renewables, distributed rooftop generation, and increasingly active demand-side resources require fundamental changes to how the grid is built and operated. The combined investment in grid modernization across major economies through 2035 is projected to exceed $5 trillion.

Transmission expansion is the single largest physical bottleneck. New high-voltage lines take a decade or more to permit and build in the United States, with siting battles, multi-state regulatory approval, and landowner negotiations creating sustained delays. The Pacific Northwest has substantial wind and hydro that cannot reach demand in California due to limited interties; the central United States produces enormous amounts of wind power that struggles to reach coastal demand. The Federal Energy Regulatory Commission issued landmark rule changes in 2023-2024 attempting to streamline interstate transmission planning, but physical project completion lags rule-making by many years.

High-voltage direct current, or HVDC, transmission is increasingly important for long-distance power transfer because it reduces line losses and allows asynchronous interconnection between grids. China has built the world's most extensive HVDC network, including lines exceeding 3,000 kilometers in length and 12 gigawatts of capacity. Europe is building substantial HVDC backbones to integrate North Sea offshore wind. The United States has historically built less HVDC than its scale would suggest, though several major projects are advancing.

At the distribution level, the rise of rooftop solar and behind-the-meter batteries is transforming traditionally one-way distribution circuits into bi-directional networks. The fundamental engineering challenge is voltage management: when distributed solar is producing more than local load, voltage rises along the feeder, potentially exceeding equipment limits and triggering inverter trip-offs that create cascading instabilities. The mitigation is smart inverter technology, with grid-supportive functions built in, plus utility supervisory control systems that can manage distributed resources at fleet scale.

The IEEE 1547-2018 interconnection standard mandates many of these smart inverter functions for new installations in the United States. Volt-VAR control, frequency response, ride-through during grid disturbances, and curtailment in response to over-voltage are now standard features in residential and commercial inverters sold for grid-tied operation. The next layer is utility-side software systems that orchestrate hundreds of thousands of distributed inverters as a coordinated resource — a Distributed Energy Resource Management System, or DERMS.

Demand-side flexibility is the third pillar of grid modernization. Historically, electricity demand was treated as inelastic — utilities had to match supply to whatever demand the grid presented. With intelligent thermostats, electric vehicle smart charging, hot water heater control, and increasingly sophisticated industrial demand response, a meaningful fraction of demand can be shifted in time. Texas, where extreme weather events have stressed the grid, has been a leading market for demand response programs that exceed 5 gigawatts of dispatchable load reduction.

Electric vehicle smart charging deserves particular attention. The average residential EV adds roughly 4-6 kilowatt-hours per day of new electricity demand, but with managed charging, that demand can be moved into off-peak hours — particularly midday solar peaks where excess generation would otherwise be curtailed, or late-night low-load hours. Several pilot programs have demonstrated that 80-90 percent of EV charging energy can be delivered during periods of low system stress when smart charging is enabled. Bidirectional charging, where EV batteries discharge back to the home or grid, is technically demonstrated and commercially emerging in 2024-2025.

The software layer underpinning the modern grid is increasingly safety-critical. Outages in 2003, 2011, and 2021 in North America were each substantially caused or worsened by software systems that didn't behave as intended under stress. Cybersecurity is a major concern, with multiple confirmed and suspected attacks on grid operational technology in the past decade. Hardening the operational technology of the grid against both natural disasters and cyberattacks is a substantial parallel investment.

Market design is evolving to reflect these new realities. Capacity markets that historically valued steady-state generation are being redesigned to value flexibility, fast response, and locational characteristics. The value of energy delivered at a particular time and place can vary by an order of magnitude in stressed grid conditions, and market designs that capture this granularity are essential for efficient investment in the right resources.

The electrification of end-uses — transportation, heating, industry — is expected to roughly double total electricity demand in most major economies by 2050. Combined with the shift to high renewable penetration, this implies that the grid must approximately quadruple its capability to deliver clean electricity reliably. The pace of this transformation will be set as much by regulatory and political processes as by technology and economics."""
    },
]

_AI_AGENTS_SOURCES: list[dict[str, str]] = [
    {
        "url": "mock://ai-agents-1",
        "title": "From Chatbots to Agents: The Architectural Shift",
        "snippet": "Why agentic AI requires fundamentally different infrastructure than chat models.",
        "content": """The transition from conversational AI assistants to autonomous AI agents represents one of the largest architectural shifts in software since the rise of mobile computing. While the underlying language models in 2025 are descendants of the same transformer architecture that powered ChatGPT in 2022, the systems built around them have diverged dramatically. A conversational chatbot is essentially a stateless function: input text, output text. An agent is a long-running system that holds state, takes actions in the world, manages plans, recovers from errors, and operates with at least partial autonomy.

The defining capability that turns a language model into an agent is tool use — the ability to call external functions, services, and databases. Early implementations of tool use through prompting were brittle. Modern agentic systems use structured function calling protocols, typically with JSON schemas declaring the tool's input and output, and language model outputs constrained to validate against those schemas. The Model Context Protocol, or MCP, introduced in 2024, has emerged as a standard for exposing tools to AI agents, with hundreds of compatible servers for everything from filesystem access to database queries to specialized domain tools.

A second defining feature is multi-step reasoning. A chatbot answers a single question. An agent decomposes a goal into sub-goals, executes them, observes results, and iterates. Patterns like ReAct (Reason-Act-Observe), Plan-and-Execute, and Reflexion have entered the standard repertoire. Production systems combine these patterns with hard-coded scaffolding that constrains the agent's planning to known-good patterns for the deployment domain.

The third defining feature is persistence. A useful agent must remember what it has done, what it has learned, and what its plans are. Persistence requirements vary from simple conversation history (chatbot territory) to rich structured state (a research agent tracking sources and findings) to long-running stateful workflows that may span weeks of real time and multiple human-in-the-loop interactions.

The economics of running agents are quite different from running chatbots. A chatbot interaction may consume a few thousand tokens. A non-trivial agentic workflow can easily consume hundreds of thousands or millions of tokens. The cost difference is two to four orders of magnitude, with corresponding implications for unit economics. Agentic AI has been compared to early cloud computing in this respect: revolutionary capability that requires careful engineering to make economically viable.

Cost per token has declined steadily, with frontier model prices roughly halving every six to eighteen months. Even so, the bulk of AI agent costs in 2025 production systems comes from inefficient context management — sending the same source material to the model repeatedly across reasoning steps because there is no externalized state to reference. Agents that aggressively externalize state to structured stores can reduce token usage by a substantial multiple compared to naive implementations.

Latency is the second economic axis. Frontier language models in 2025 take 200-500 milliseconds for a quick response and tens of seconds for a long generation. Agentic workflows that require five, ten, or more model calls in sequence accumulate substantial wall-clock time. Approaches to reduce this include parallelization of independent tool calls, caching of repeated invocations, smaller specialized models for sub-tasks where the frontier model is overkill, and pre-computation of common patterns.

Reliability is the third economic axis. Frontier language models hallucinate, miss instructions, and produce incorrect tool invocations at non-trivial rates. Production agentic systems implement multiple layers of validation: schema validation on outputs, semantic checking against known constraints, escalation to humans when confidence is low, and idempotent retry logic when transient errors occur. Building these layers reliably has emerged as one of the central engineering challenges in productionizing agents.

The infrastructure required to support production agents looks more like a distributed system platform than like a chatbot service. Components include: a workspace or memory store with versioning and rollback; a tool gateway with authentication, caching, audit, and idempotency; an observability layer capturing every reasoning step; identity management with capability-scoped credentials; coordination primitives for multi-agent collaboration; and orchestration for long-running workflows. Many of these components have analogues in human-oriented software infrastructure but require purpose-built versions optimized for agent use patterns.

The current state of the agent infrastructure space is fragmented. Various startups and open-source projects are attempting to fill different niches: workspace and memory, tool gateways, agentic orchestration, observability. The industry has not yet converged on dominant designs in most layers, and the eventual structure of the agent infrastructure stack remains an open question. Some observers expect consolidation around a handful of comprehensive platforms; others expect a more modular ecosystem with specialized providers in each layer.

What is clear is that the addressable market for agent infrastructure is large and growing rapidly. Goldman Sachs, McKinsey, and other analysts project that AI agents will be operating an increasing fraction of business processes through the late 2020s, with cumulative spending on agent infrastructure measured in tens of billions of dollars by 2030. The platforms and standards that emerge in the next few years will shape the economics of AI deployment for the following decade."""
    },
    {
        "url": "mock://ai-agents-2",
        "title": "Tool Use, Function Calling, and the Model Context Protocol",
        "snippet": "How agents talk to external systems, and the rise of MCP as a standard.",
        "content": """The capability of language models to call external tools transformed AI from a question-answering technology into a general-purpose action-taking system. The history of this capability is short — large-scale function calling debuted in production language models in mid-2023 — but its impact has been transformative across the industry. Understanding how tool use works under the hood, and where the leverage points are, is essential for building production AI agents.

The fundamental mechanism is straightforward. The language model is given, alongside the user's question, a structured description of available tools. Each tool description includes a name, a natural-language description, and a schema for the tool's input arguments. The model is trained or instructed to emit, when appropriate, a structured response indicating which tool to call and with what arguments. The host application parses this structured output, executes the tool, and returns the result to the model in subsequent turns.

The reliability of this mechanism depends on three things: the quality of the model's training on structured outputs, the clarity of the tool descriptions, and the design of the schemas. Modern frontier models are highly reliable at producing valid structured outputs when the schemas are well-formed. The failure modes that remain are mostly around the model's tool selection (calling the wrong tool, or calling tools when none is needed) and around argument quality (getting most arguments right but one slightly wrong).

The Model Context Protocol, or MCP, introduced by Anthropic in late 2024, has rapidly become the dominant standard for exposing tools to AI agents. Before MCP, every agentic system implemented its own tool integration approach. Tool authors had to integrate with each agent platform separately. Agent developers had to write custom integration code for each tool. The combinatorial explosion was a major drag on the ecosystem.

MCP defines a small but well-thought-out protocol. An MCP server exposes a list of tools, each with a JSON schema for its inputs and outputs and a natural-language description optimized for AI consumption. Clients — typically AI agents or development environments — connect to MCP servers and use them through the standard protocol. The protocol supports both streaming HTTP and stdio transports, allowing MCP servers to be deployed as cloud services or as local processes.

The MCP ecosystem grew rapidly through 2025. By mid-year, hundreds of public MCP servers existed for filesystem access, database connectors (PostgreSQL, MongoDB, Snowflake, BigQuery), SaaS applications (GitHub, Linear, Asana, Notion, Slack), web access (Brave Search, Tavily, Exa), specialized domains (Stripe for payments, Twilio for communications, AWS for cloud operations), and many more. Tools that had previously required custom integration could now be plugged into any MCP-compatible agent platform with minimal effort.

This standardization unlocked several important second-order effects. First, the cost of building an agent dropped substantially: developers could compose existing MCP servers rather than writing every tool integration from scratch. Second, agents became more portable across runtimes — an agent designed for one platform could often be moved to another without rewriting tool integration code. Third, the security model became clearer, with the connection between agent and tool now mediated by a well-defined protocol that could be audited and constrained.

But MCP also exposed the limits of the simple tool-calling model. As agents work with larger numbers of tools — dozens, hundreds, or thousands — providing all tool descriptions in every prompt becomes prohibitively expensive in tokens. Solutions to this include semantic tool selection (using a smaller model or embedding-based retrieval to narrow down to relevant tools first) and hierarchical tool organization (with meta-tools that introduce sub-tools as needed).

A second limit relates to authentication. Every meaningful tool requires some form of authentication: an API key for a SaaS service, an OAuth token for user data, a database credential. Distributing these credentials safely to agents — without leaking them in logs, embeddings, or reasoning traces — is a substantial engineering problem. The emerging answer is to centralize credentials in a tool gateway that mediates all calls and never exposes credentials to the agent itself.

A third limit is observability. Every tool call should be logged for audit, debugging, and cost attribution purposes. The volume of tool calls in a busy agent system can be substantial — tens or hundreds of thousands per agent per day in some applications. Building observability infrastructure that scales to this volume, while remaining queryable and cost-effective, is non-trivial.

A fourth limit is caching and idempotency. Many tool calls are read-only and their results are stable for some duration. Caching these calls can produce dramatic cost and latency improvements, particularly for agent workflows that involve repeated reasoning over the same source material. Identifying which tool calls are safe to cache, and for how long, requires either tool-author cooperation (declarations on the tool registration) or sophisticated heuristic analysis.

The future of tool use in AI agents likely involves continued evolution of the underlying protocol — MCP itself has gone through multiple revisions in its short life — alongside increasingly sophisticated infrastructure layers that sit between the agent and the raw tool. Tool gateways, semantic routing, capability tokens, and observability layers are all emerging as standard components of production agent stacks."""
    },
    {
        "url": "mock://ai-agents-3",
        "title": "Memory, State, and the Agent Workspace",
        "snippet": "How agents maintain coherent state across long-running tasks.",
        "content": """One of the central engineering challenges of AI agents is memory. The underlying language model is fundamentally stateless — each invocation receives a context window of tokens and produces an output. To behave like an agent, the system must somehow accumulate state across many invocations: what has been done, what has been learned, what is planned, what intermediate results have been computed.

The simplest memory model is the conversation history. Each new model invocation includes the full history of prior user messages, model responses, and tool call results. This works for short interactions but breaks down quickly as the conversation grows. Context windows in 2025 frontier models extend to 200,000 to 1 million tokens, which sounds large but fills up rapidly when an agent has read a few documents, executed several tool calls, and reasoned through multiple steps.

The cost dimension is severe. Pricing for frontier models scales linearly with context length on input tokens. An agent that adds 10,000 tokens to its context every reasoning step, and runs for 20 reasoning steps, sends approximately 200,000 tokens just on the final step. The cumulative cost across all steps is even larger, because each step also pays for all prior steps' context. For a hypothetical $3 per million input tokens, a single such agent run can cost $0.50 to $5 depending on configuration.

The latency dimension is also problematic. Larger context windows take measurably longer to process. While the per-token throughput of modern inference systems is high, the prefill phase that processes the entire input scales with input size. A million-token prompt takes meaningfully longer to start producing output than a thousand-token prompt.

The robustness dimension is perhaps most subtle. As context grows, models exhibit declining performance on retrieval tasks — finding the relevant earlier information needed for the current reasoning step. The phenomenon, sometimes called lost in the middle, has been documented across multiple model families. Effectively, an agent that relies on its conversation history for memory degrades in capability as the conversation grows.

The response from the field has been to externalize agent memory into structured stores accessed through tools. The agent's working set becomes much smaller — just the current focus of attention — while the bulk of accumulated state lives in databases, vector stores, file systems, or specialized agent workspace systems. The agent reads what it needs by key or query, rather than by always having everything in context.

Several patterns have emerged. The simplest is a key-value store, where the agent writes structured findings under named keys and retrieves them later. Vector databases allow semantic retrieval, where the agent queries by concept rather than exact key. Filesystem-based stores allow rich file-based artifacts. Most production systems combine multiple of these patterns.

The next sophistication is versioning. As the agent accumulates state, having a history of prior versions is invaluable — for debugging when things go wrong, for snapshot-and-rollback semantics when an exploration doesn't pan out, and for audit trails that satisfy compliance requirements. Versioned state stores, with snapshot and branch primitives, are increasingly recognized as a core component of agent infrastructure.

The consequence of this architectural shift is that production agent systems look less like simple language model integrations and more like distributed database applications with language models as a clever query layer. The patterns of database design — schema, indexing, transactions, consistency models — are being rediscovered and adapted for agent workloads.

A particularly interesting question is the interaction between the agent's working memory (in-context) and its long-term memory (externalized). The most cost-efficient agents send only the minimum necessary content to the model in each call. But determining what is necessary requires either explicit programming or sophisticated meta-reasoning. Hard-coded scaffolds work well for known workflows but limit flexibility. Letting the model itself decide what to load is more flexible but harder to make cost-efficient.

A productive design pattern is the structured workspace, where the agent's externalized state has a known schema that the agent can navigate by key. The agent reads by key (cheap, predictable), processes a small piece of state at a time, and writes results back by key. This pattern fits naturally with the kinds of decomposed task structures that agents handle well, and the cost profile is predictable and bounded.

Some agent frameworks have begun providing workspace primitives as first-class infrastructure. These typically expose KV operations, file operations, snapshot and branch semantics, and integration with the agent's tool-calling mechanism. The bet is that having proper workspace primitives — with the right ergonomics for AI agents specifically — produces dramatic improvements in both cost and capability over ad-hoc memory implementations.

Multi-agent scenarios add another dimension. When multiple agents collaborate, they need shared memory with appropriate isolation. The patterns from distributed systems — locks, transactions, consensus — apply, but the access patterns are different from human-operated systems. Agent-to-agent handoffs, where agent A completes some work and agent B picks up from a known state, motivate snapshot-based handoff protocols that look more like git branches than like traditional message queues."""
    },
    {
        "url": "mock://ai-agents-4",
        "title": "Multi-Agent Systems and Coordination Patterns",
        "snippet": "How multiple agents collaborate, hand off work, and avoid conflicts.",
        "content": """Single-agent AI systems are now widely deployed, but the next frontier is multi-agent systems where multiple specialized agents collaborate on tasks more complex than any single agent could handle alone. These systems introduce coordination problems that single-agent designs do not face: how to divide work, how to share state, how to avoid conflicts, how to handle failures, how to maintain coherent behavior across the whole system.

The motivations for multi-agent designs are straightforward. Different sub-tasks have different optimal models — some need frontier-quality reasoning, others can use cheaper specialized models. Different sub-tasks have different optimal tool sets. Specialization improves capability in the same way that human teams do. Parallelization across independent sub-tasks can dramatically reduce wall-clock time.

The simplest multi-agent pattern is hierarchical: a planning agent decomposes a task into sub-tasks and dispatches them to worker agents. The workers complete their sub-tasks and return results to the planner, which synthesizes them. This pattern is well-suited to tasks with clear decomposition, like a research agent dispatching individual source-fetch tasks or a coding agent dispatching individual function-implementation tasks.

Hierarchical patterns work well when sub-tasks are largely independent. They struggle when sub-tasks have complex interdependencies that require iteration. The next pattern, the team or peer-to-peer pattern, has multiple agents collaborating without a fixed hierarchy. This more closely resembles human team collaboration but introduces serious coordination challenges.

Communication protocols are the first design choice in multi-agent systems. Synchronous request-response works for hierarchical patterns. Message-passing channels with persistence work better for asynchronous patterns where one agent might pick up work the other has set down. Shared workspace patterns, where multiple agents read and write a common state store, work well when the work is genuinely collaborative on shared artifacts.

State sharing introduces concurrency hazards. If two agents simultaneously modify the same state, they can produce inconsistent results. The classical solutions from distributed databases — locks, transactions, optimistic concurrency control — apply directly, with adaptations for the agent context. A common pattern is for each agent to work on its own branch of a versioned workspace, with explicit merge points where work is reconciled.

Fault tolerance becomes more complex in multi-agent settings. If one agent crashes, the others may be blocked waiting on its output. Production multi-agent systems implement timeout handling, partial-result recovery, and explicit retry logic. Persistent workflows that survive across process restarts are increasingly considered table stakes for production deployment.

Cost management is another design dimension. Multi-agent systems can multiply token costs: every coordination message, every shared context, every retry adds tokens. Naive implementations of multi-agent patterns can easily consume an order of magnitude more tokens than a well-engineered single-agent solution to the same problem. The opportunity is that, with care, multi-agent systems can also produce better results — solving problems that single agents cannot.

Specific patterns that have emerged as productive include: the critic pattern, where one agent does work and another critiques it, often reducing hallucination and quality issues; the debate pattern, where agents argue different positions and a third synthesizes the best ideas; the supervisor pattern, where a stronger model oversees the work of weaker but cheaper models; and the assembly-line pattern, where multiple agents perform different stages of a pipeline in sequence.

Trust and identity are subtle issues in multi-agent systems. When multiple agents share a workspace, can they trust each other's writes? In adversarial scenarios — for instance, an agent serving one user interacting with an agent serving another — trust cannot be assumed. Capability-token approaches, where each agent operates with explicitly scoped permissions, are emerging as a common solution.

The infrastructure to support production multi-agent systems is more demanding than for single-agent systems. Channel implementations need to be persistent, ordered, and replayable. Workspace systems need branch and merge semantics. Lock managers need to be correct under concurrent access. Workflow engines need to coordinate long-running processes. These layers are increasingly being recognized as agent infrastructure rather than as bespoke components of each agent application.

The practical challenge for agent developers is choosing the right abstraction level. Building everything from raw language model calls and database queries gives maximum flexibility but takes substantial engineering effort. Using high-level multi-agent frameworks gives faster development but locks in architectural assumptions that may not match the application. The middle ground — using composable infrastructure primitives that can be combined into different patterns — appears to be where most production systems are converging.

Looking forward, multi-agent systems are likely to become more common as the capability bar for what single agents can do continues to rise. Currently, many applications that could in principle be multi-agent are implemented as single-agent because the coordination cost is too high. As the cost of coordination falls — through better infrastructure, better protocols, better tooling — the equilibrium will shift toward more multi-agent designs. The infrastructure layer that enables this efficiently is likely to be one of the major value-capture points in the agent technology stack."""
    },
    {
        "url": "mock://ai-agents-5",
        "title": "Observability, Cost, and the Operations of Production Agents",
        "snippet": "Why running an agent in production is fundamentally different from running a service.",
        "content": """Operating AI agents in production is a substantial discipline distinct from operating traditional software services. The behavior of an agent is non-deterministic in ways that traditional services are not; the cost dynamics scale with model invocations rather than with simple compute and storage; failure modes include hallucination, prompt injection, and sustained loops that have no equivalent in conventional software. Building the operational infrastructure to manage these systems at scale is one of the central challenges facing organizations deploying AI in production.

The first observation about production agents is that the unit economics are visible in a way that traditional service economics are not. Every model invocation has a measurable token count and corresponding cost. Aggregating these costs by user, by workflow, by feature, gives unusually granular cost attribution data. Modern agent operations practices treat token expenditure as a primary KPI, with daily or hourly tracking and per-feature attribution.

The second observation is that failure modes are more varied. Traditional services fail in well-understood ways: timeouts, memory exhaustion, dependency unavailability, deserialization errors. Agents fail in those ways too, but they also fail by hallucinating tool calls that don't make sense, by getting stuck in loops, by being prompt-injected into producing unsafe outputs, by generating valid-but-wrong structured outputs that pass schema validation but fail downstream. The taxonomy of agent failure modes is still being mapped.

Observability for agents requires capturing the full reasoning trace — not just inputs and outputs but the model's internal reasoning, the tool calls it considered, the alternatives it rejected. This is partly available through structured outputs (chain-of-thought, draft-tool-calls) and partly through carefully designed prompting that asks the model to externalize its reasoning. Observability platforms specialized for agents have emerged in 2024-2025 to handle this data type, with dashboards that visualize agent traces in ways that traditional APM tools do not.

Audit logs of every tool call are increasingly considered a baseline requirement for production agent deployment. The audit log captures: what tool was called, with what arguments, what result was returned, how long it took, what it cost. Aggregated audit data drives cost attribution, debugging, security review, and regulatory compliance. In domains like financial services and healthcare, agent audit logs are subject to specific retention and integrity requirements.

Cost optimization is a major focus area. Several techniques compound in well-engineered agent systems. Prompt caching, where the input context is cached server-side between calls, can eliminate redundant token costs for stable preambles. Tool gateways that cache results of idempotent calls eliminate redundant external service costs. Smaller specialized models for sub-tasks reduce per-call costs. Semantic compression, where verbose intermediate state is replaced with structured summaries, reduces context size. The cumulative effect of these techniques on a real agent workload often exceeds 50 percent cost reduction compared to a naive baseline.

Reliability engineering for agents extends traditional SRE practices with agent-specific concerns. Service level objectives for agent systems often include not just latency and availability but also accuracy or task-success rates. Measuring task success requires investment in evaluation frameworks: graded test sets, human review of samples, automated checks against ground-truth data when available. Tying the SLO regime to cost tracking allows organizations to make informed trade-offs between cost and quality.

The deployment pipeline for an agent has unfamiliar elements. Traditional code goes through review, test, deployment. An agent has prompt changes, tool change, model change, and underlying code change as separate dimensions, each with its own evaluation requirements. Some changes — switching to a different underlying model, for instance — require comprehensive regression testing across the full evaluation suite, because the model may behave subtly differently across many cases.

Security concerns for production agents include traditional service security plus agent-specific concerns. Prompt injection attacks, where adversarial content in tool inputs causes the agent to misbehave, are a persistent and evolving threat. Defenses include input sanitization, output validation, capability-scoped tools that limit the blast radius of compromised reasoning, and explicit detection of injection attempts. Tool gateway architectures, where credentials are held by the gateway and never exposed to the agent itself, provide a strong containment model for credential theft.

Capacity planning for agents involves modeling not just compute and storage but also model API rate limits and per-call latencies. As agent workflows can involve many sequential model calls, end-to-end latency is sensitive to per-call latency in a way that simple service architectures are not. Patterns to mitigate include parallelizing independent calls, using cheaper models for non-critical paths, and pre-computing common results.

The operational disciplines around production agents are evolving rapidly. The agent operations role — sometimes called AgentOps or AI operations engineer — has emerged as a specialty within DevOps and SRE teams. The required skill set combines traditional operations expertise with familiarity with language models, agent frameworks, evaluation methodologies, and the unfamiliar economic dynamics of token-based billing.

The infrastructure to support these operations is itself a significant investment. Organizations operating agents at scale typically build or buy: cost attribution platforms, evaluation frameworks, audit and observability systems, prompt and tool management systems, and incident-response playbooks specific to agent failures. As the agent ecosystem matures, these capabilities are increasingly being provided as managed services rather than built in-house, with the agent infrastructure layer emerging as a substantial software market in its own right."""
    },
]

_CLIMATE_POLICY_SOURCES: list[dict[str, str]] = [
    {
        "url": "mock://climate-policy-1",
        "title": "The Inflation Reduction Act: Architecture and Early Outcomes",
        "snippet": "The largest climate investment in U.S. history, three years in.",
        "content": """The Inflation Reduction Act, signed into law in August 2022, committed the United States to its largest climate investment in history — a package of tax credits, direct subsidies, and incentive programs nominally valued at $369 billion over ten years. By the time of its third anniversary, the actual fiscal flows associated with the IRA appeared to be tracking substantially higher than initial estimates, with some analyses suggesting the full ten-year cost may exceed $1 trillion in foregone tax revenue.

The architecture of the IRA differs significantly from earlier climate legislation. Rather than carbon pricing, mandates, or top-down regulation, the IRA primarily uses tax credits — many of them refundable, transferable, or both — to subsidize private investment in low-carbon technology. The theory is straightforward: by making it economic to build and deploy clean technology, market forces will accelerate the energy transition without the political costs of carbon taxation.

The major provisions span generation, manufacturing, end-use electrification, and emerging technologies. Production tax credits for clean electricity generation extend through 2032, with bonus credits for projects in disadvantaged communities, projects using American-made components, and projects in energy-transition zones. Manufacturing tax credits — the so-called 45X credit — pay producers of solar cells, wind turbines, batteries, and other clean technology components per unit of output, regardless of whether the buyer is in the United States.

The 30D consumer tax credit for electric vehicles, with strict requirements on assembly location and battery sourcing, has been credited with both accelerating EV adoption in the U.S. and substantially reshaping the U.S.-China supply chain dynamics in critical minerals. The credit's complex eligibility rules — and their phase-in over several years — created substantial market disruption as the eligible vehicle list shifted.

The hydrogen production tax credit, 45V, offers up to $3 per kilogram for hydrogen produced with very low carbon intensity. Final rules issued in late 2024 imposed strict requirements on the additionality of renewable electricity, hourly matching of generation to consumption, and geographic deliverability. These rules, intended to prevent the credit from subsidizing hydrogen with high indirect emissions, also significantly constrain which projects can qualify and have led to industry pushback.

The Methane Emissions Reduction Program imposed direct fees on methane emissions from oil and gas operations exceeding specific thresholds, with phased implementation through 2025-2026. This is the closest thing to a federal carbon price in the United States, though limited to a specific sector and pollutant.

Tracking actual deployment three years in, several patterns are clear. Manufacturing investment in the U.S. has surged dramatically, with hundreds of billions of dollars announced for battery factories, solar manufacturing, EV assembly, and steel mills using clean processes. The geographic distribution favors red states with lower labor costs and faster permitting; this has created an interesting political dynamic where the law's beneficiaries are heavily concentrated in districts represented by legislators who voted against it.

Deployment of utility-scale clean generation has accelerated, though more slowly than the manufacturing build-out. Permitting and interconnection bottlenecks have meant that many announced projects have not yet broken ground. The grid interconnection queue at major U.S. operators contains over a terawatt of proposed clean generation as of 2025, with typical wait times exceeding three years.

EV adoption in the U.S. has grown but remains below the trajectory needed to meet stated emissions reduction goals. Sales of plug-in vehicles reached approximately 12 percent of new light-duty sales in 2024 and were on track for somewhat higher shares in 2025. Concerns about charging infrastructure, regional disparities in adoption, and political backlash against EV mandates have created headwinds. Some major automakers have scaled back EV investment plans relative to the announcements of 2022-2023.

Emissions outcomes are more difficult to assess in the short window since enactment. The most credible analyses, from groups like the Rhodium Group and Princeton's REPEAT project, suggest the IRA puts the U.S. on track to reduce emissions by roughly 35-40 percent below 2005 levels by 2030, falling short of the original 50-52 percent commitment. Whether the gap can be closed through additional policy, faster-than-expected technology cost declines, or higher-than-expected deployment depends on factors that remain uncertain.

The political durability of the IRA has been a continuing question. While portions of the law are technically subject to repeal through legislation, repealing them would require both chambers of Congress and a willing president. The geographic concentration of IRA-funded projects in Republican districts has emerged as a significant constraint on full repeal, though incremental changes — tightened eligibility, accelerated phase-outs, regulatory implementation choices — remain politically possible.

Looking forward, the IRA's effects will continue to compound through the late 2020s as projects move from announcement to construction to operation. The full assessment of the law's success will depend on how much manufacturing capacity actually comes online, how much of that capacity stays competitive after subsidy phaseouts, and how the law interacts with future policy choices both domestically and internationally."""
    },
    {
        "url": "mock://climate-policy-2",
        "title": "Carbon Pricing: From Theory to Practice in 2025",
        "snippet": "The European ETS, the U.S. patchwork, and the patchwork of national approaches.",
        "content": """Carbon pricing — putting a price on greenhouse gas emissions through either a tax or a cap-and-trade system — is the policy approach that economists have advocated most strongly for decarbonization. The theoretical case is elegant: a uniform price on emissions ensures that abatement happens where it is cheapest, that consumers face accurate prices that reflect environmental costs, and that the policy is economically efficient. The practical reality has been considerably messier.

The European Union Emissions Trading System, or EU ETS, is the world's largest and longest-running carbon market, in operation since 2005. After two decades of incremental reform, the EU ETS in 2025 covers approximately 40 percent of EU emissions across power generation, large industrial facilities, intra-EU aviation, and recently expanded to maritime transport and an emerging separate market for buildings and road transport (ETS2).

The price of EU carbon allowances reached approximately €100 per ton CO2 by mid-2024 before easing to around €70-80 per ton in 2025 amid economic slowdown and successful decarbonization in covered sectors. The price level has been sufficient to drive substantial emissions reductions in the power sector — coal generation in Germany, Poland, and elsewhere has fallen dramatically. The price has been less effective at driving industrial decarbonization, where many facilities receive free allowances under transitional arrangements, and where the underlying technology for low-carbon production (such as green hydrogen for steel) is not yet cost-competitive even at €100 per ton.

The Carbon Border Adjustment Mechanism, or CBAM, applies a carbon price to imports of carbon-intensive products entering the EU. CBAM transitioned to its first reporting phase in late 2023 and to financial obligations in 2026. The mechanism is intended to prevent carbon leakage — the relocation of emissions-intensive industry to jurisdictions with weaker climate policy — but has generated substantial international friction with major trading partners including China, India, and Turkey.

In the United States, federal carbon pricing legislation has consistently failed to advance in Congress despite being reintroduced regularly. State-level programs cover a meaningful share of U.S. emissions: California's cap-and-trade program, the Regional Greenhouse Gas Initiative covering eleven northeastern states, and Washington state's Climate Commitment Act. California's program, the largest single U.S. state effort, prices around 80 percent of the state's emissions and recycles substantial revenue into clean energy and climate justice programs.

The patchwork nature of U.S. carbon pricing creates complications. Manufacturers in capped states face costs that competitors in uncapped states do not, raising leakage concerns. Border adjustments at state lines are more politically and legally fraught than at international borders. The overall U.S. effective carbon price, weighted by emissions covered, is meaningfully below the EU level.

Canada implemented a federal carbon backstop, requiring all provinces to either adopt a carbon price meeting federal minimum standards or accept the federal program. The federal price reached CAD$80 per ton in 2024 and is scheduled to rise to CAD$170 per ton by 2030. The political durability of the program has been a continuing question, with significant opposition from prairie provinces and political pressure to weaken implementation.

China launched its national emissions trading system in 2021, initially covering only the power sector. Coverage has expanded to additional industries through 2024-2025. The price has remained low, around the equivalent of $10 per ton, and the program's effectiveness in driving emissions reductions has been limited compared to EU and Canadian programs. Chinese officials have framed the early stages as building experience rather than driving aggressive abatement.

A meta-trend across carbon pricing programs has been the rise of complementary policies. Pure carbon pricing has consistently encountered political and equity headwinds when not paired with other policies that address distributional concerns. Revenue recycling — using carbon price revenue to provide direct rebates to households or to fund clean energy investment — has become standard practice. Sector-specific complementary policies — vehicle emissions standards, building codes, renewable portfolio standards — have generally accompanied carbon pricing rather than being replaced by it.

The empirical record on carbon pricing as of 2025 supports a nuanced conclusion. Carbon pricing at moderate levels (€50-100 per ton) is effective at driving decarbonization in sectors with cost-competitive low-carbon alternatives — most notably power generation and some industrial processes. It is less effective in sectors where alternatives remain expensive, such as heavy industry and aviation. Carbon pricing is generally well-tolerated politically when revenues are recycled to households, less so when used for general government purposes. International coordination remains difficult; the World Bank tracks dozens of carbon pricing instruments globally with prices spanning multiple orders of magnitude.

The question of where carbon pricing fits in the next decade of climate policy remains contested. Some advocates continue to argue that higher and more universal carbon pricing is the most efficient path to decarbonization. Others, including parts of the policy community in the United States, have shifted toward an industrial-policy approach centered on direct subsidies, mandates, and standards. The actual policy mix in most major economies combines multiple approaches, with the relative weight of each varying by jurisdiction and sector."""
    },
    {
        "url": "mock://climate-policy-3",
        "title": "International Climate Negotiations and the Paris Agreement",
        "snippet": "Where the global climate framework stands a decade after Paris.",
        "content": """The Paris Agreement, adopted in December 2015 and now nearly a decade old, established the architecture of contemporary international climate policy: country-determined commitments (Nationally Determined Contributions, or NDCs) within a framework of regular reporting, review, and ratcheting up of ambition over time. The agreement entered into force in 2016 and has been ratified by virtually all nations. As 2025 unfolds, the question is whether the architecture is producing the climate outcomes its designers intended.

The headline target of the Paris Agreement is to hold global warming "well below 2 degrees Celsius" above pre-industrial levels and to "pursue efforts" to limit warming to 1.5 degrees Celsius. The 1.5 degrees target was strengthened over time as scientific assessment indicated that warming above this level produces substantially greater climate impacts than warming to 2 degrees. The Intergovernmental Panel on Climate Change's 2018 Special Report on 1.5 degrees gave the higher-ambition target operational definition: roughly 45 percent emissions reduction below 2010 levels by 2030 and net zero around 2050.

The current trajectory is meaningfully above these benchmarks. Aggregated NDCs, even if fully implemented, project warming of approximately 2.5-2.8 degrees Celsius by 2100. Actual policies implemented to date project warming closer to 3 degrees Celsius. The gap between aspirational targets, NDC commitments, and implementation has been the central political fact of the Paris architecture.

The structure of the agreement was designed to address this gap through periodic global stocktakes — comprehensive reviews of collective progress — followed by enhanced NDCs from each party. The first global stocktake, completed at COP28 in Dubai in 2023, produced a consensus document calling for a transition away from fossil fuels in energy systems, tripling of renewable energy capacity by 2030, and doubling of energy efficiency improvements. The political significance was substantial — the first explicit reference to fossil fuel transition in a UNFCCC consensus text — though the operational consequences for individual country commitments remained limited.

Subsequent conferences have continued to grapple with the gap. COP29 in Baku in 2024 focused on climate finance, producing a new collective quantified goal of $300 billion per year in climate finance from developed to developing countries by 2035, with aspirations to reach $1.3 trillion through additional public and private flows. The agreement was widely characterized as inadequate by developing-country negotiators relative to estimated needs of $1 trillion per year or more.

COP30 in Belém, Brazil in late 2025 has been positioned as a moment for significantly enhanced NDCs in advance of the next round of commitments due in early 2025-2026 for the post-2030 period. The political dynamics surrounding COP30 are challenging, with the United States having withdrawn from the Paris Agreement at the start of 2025 under the new administration, Russia continuing to contest international climate cooperation in the context of its broader geopolitical posture, and significant challenges in major emerging economies maintaining ambition under economic pressures.

The U.S. withdrawal from the Paris Agreement in early 2025 created substantial complications for the international architecture. Under the Paris Agreement's design, the United States cannot complete withdrawal until 2026, and re-entry under a future administration would be relatively rapid. Subnational actors in the United States — states, cities, businesses — formed the U.S. Climate Alliance and similar organizations to maintain commitments parallel to the federal exit, and their aggregated commitments cover roughly half of U.S. emissions and economic output.

Climate finance flows have grown but remain well below the levels developing countries argue are required. Actual flows from developed countries reached approximately $115 billion in 2022 (the most recent year for which complete data exists), modestly exceeding the prior $100 billion goal but well short of needed levels. The composition of climate finance has been a continuing point of contention, with developing countries arguing that loans should not count the same as grants, and that adaptation finance has been chronically under-prioritized relative to mitigation finance.

The role of carbon markets under the Paris Agreement has continued to evolve. Article 6 of the agreement establishes mechanisms for international transfer of mitigation outcomes — essentially carbon credits — between countries. The operational rules for Article 6 were finalized at successive COPs through 2021-2024, but actual market activity has been limited by methodological disputes, integrity concerns over older vintage credits, and uncertainty about long-term demand. Voluntary carbon markets, separate from the Article 6 framework, have been through significant turbulence with multiple major credit scandals.

The architecture of international climate cooperation faces several structural challenges as the late 2020s approach. The bottom-up nature of NDC-based commitment is producing aggregate ambition well below collectively agreed targets. The differentiation between developed and developing country obligations, established in earlier agreements, fits awkwardly with the rise of major emerging economies as significant emitters. The intersection of climate policy with trade policy, particularly through CBAM-style border adjustments, is creating tension between climate cooperation and trade cooperation regimes. And the political durability of climate commitments in major economies has proven less robust than negotiators hoped during the 2015 Paris moment.

Whether the Paris Agreement architecture can deliver outcomes consistent with stated targets, or whether it requires fundamental reform, is the central open question of the next decade of international climate policy."""
    },
    {
        "url": "mock://climate-policy-4",
        "title": "Subnational Climate Policy: States, Cities, and the Bottom-Up Movement",
        "snippet": "How U.S. states and global cities are leading where federal action lags.",
        "content": """Subnational governments — U.S. states, Canadian provinces, German Länder, U.K. devolved administrations, cities of every size globally — have emerged as significant actors in climate policy, often leading in domains where their national governments have been slow to act. The United States provides the most striking case: substantial climate policy activity at state level alongside repeated cycles of federal commitment and withdrawal. But the pattern recurs internationally, with major cities and subnational governments collectively producing meaningful policy outputs that complement and sometimes exceed national commitments.

In the United States, California has been the single most important subnational climate actor. California's cap-and-trade program, regulatory programs targeting transportation emissions, building decarbonization mandates, and clean electricity standards collectively cover the state's economy in ways that approximate the regulatory environment of leading European countries. California's automotive emissions standards, granted through a Clean Air Act waiver and adopted by approximately a dozen other states, have driven manufacturer behavior across the entire U.S. market.

The Regional Greenhouse Gas Initiative, or RGGI, covers eleven northeastern states with a power-sector cap-and-trade system. RGGI launched in 2009 and was the first mandatory carbon market in the United States. Although coverage is limited to electricity generation, the program has been associated with substantial emissions reductions in covered states and the proceeds from allowance auctions have funded energy efficiency programs and clean energy investment.

Washington state's Climate Commitment Act, implemented in 2023, created a state-wide cap-and-trade program covering approximately 75 percent of state emissions. The program has been politically contested, surviving an attempted ballot-initiative repeal in 2024. Other western states including Oregon and New Mexico have implemented or are considering similar programs.

Beyond cap-and-trade, U.S. states have been active across the policy spectrum. Renewable portfolio standards have been the most widespread policy, with approximately 30 states requiring some level of clean electricity by various target dates. Building decarbonization policies, including bans on natural gas hookups in new construction, have spread from California to other progressive jurisdictions and back through the political pendulum as some early adopters faced backlash. Vehicle emissions standards, building energy codes, methane regulations on oil and gas operations, and clean transportation funding have all seen substantial state-level action.

The U.S. Climate Alliance, formed during the first Trump administration's withdrawal from the Paris Agreement, has continued through subsequent political cycles. As of 2025, the alliance includes approximately 25 states representing roughly 60 percent of U.S. GDP and around half of U.S. emissions. The alliance has functioned as both a political forum and a coordination mechanism for technical work on issues like building codes, methane regulation, and clean transportation.

City-level climate policy has been particularly active globally. The C40 network of major world cities, founded in 2005, has grown to include approximately 100 cities representing over a quarter of global GDP and a substantial share of global emissions. C40 cities collectively pursue emissions reduction targets aligned with the Paris Agreement's 1.5 degree target. Implementation tools include municipal building codes, public transportation investment, low-emission zones for vehicles, urban forest initiatives, and procurement requirements for city operations.

European cities have led many specific innovations. London's congestion charge and ultra-low emission zone, expanded over multiple iterations, have substantially reduced central-London vehicle emissions. Copenhagen's commitment to carbon-neutral status by 2025 (the target was technically missed but performance is close) has driven substantial innovation in district heating, building retrofits, and active transportation. Barcelona's superblock program restructures street networks to prioritize pedestrians and reduce vehicle dominance.

Asian cities, particularly in China, have taken different approaches that combine climate goals with broader urban-development objectives. Shenzhen's electrification of its bus fleet — over 16,000 electric buses by 2018 — was an early demonstration of large-scale fleet electrification. Shanghai and Beijing have implemented vehicle-quota systems that direct new vehicle purchases toward electric and plug-in hybrid models. Smart-city investment programs in major Chinese cities incorporate energy efficiency and renewable integration as standard components.

The relationship between subnational and national policy is complex. In some cases, subnational action precedes and influences national policy — California's automotive standards and ambitious building codes have shaped U.S. national rules and have influenced markets globally given California's economic scale. In other cases, subnational action substitutes for absent national action — the U.S. Climate Alliance's role during federal disengagement is the canonical case. In still other cases, national action constrains or preempts subnational policy, as happened with various Trump administration efforts to restrict state authority on automotive emissions.

The political dynamics of subnational climate policy differ from national. Local governments are often closer to climate impacts — flooding, heat waves, wildfires, drought — that affect their constituents directly. They are also closer to the implementation challenges and equity questions that arise from climate policy. Local climate policy tends to be less polarized than national policy, with significant action even in jurisdictions with conservative state governments when the policies are framed around energy security, economic development, or pollution reduction rather than climate change directly.

Looking forward, the role of subnational climate action seems likely to continue and to deepen. Major-economy national governments cycle in and out of strong climate posture; subnational governments provide an important continuity layer. Climate finance, traditionally framed around national-to-national transfers, is increasingly directed toward subnational actors — particularly cities — that are closer to the actual deployment of climate solutions. The institutional architecture for subnational climate cooperation, while less formal than the UNFCCC, has become a substantial parallel system of climate governance."""
    },
    {
        "url": "mock://climate-policy-5",
        "title": "Carbon Removal: From Theory to Industrial Reality",
        "snippet": "The state of negative-emissions technology and policy.",
        "content": """Carbon dioxide removal — actively pulling CO2 out of the atmosphere — has shifted from a theoretical component of long-term climate scenarios to an industrial activity with a small but growing physical footprint. The change has been driven by both the increasing recognition that emissions reductions alone will be insufficient to meet temperature targets and by improved economics for several removal pathways.

The Intergovernmental Panel on Climate Change's pathways for limiting warming to 1.5 degrees Celsius typically include cumulative carbon removal of 100-1000 billion tons of CO2 over the 21st century. The wide range reflects uncertainty about how aggressively emissions can be reduced; the more emissions are reduced, the less removal is required. Even in the most aggressive emissions-reduction scenarios, some removal is essentially unavoidable to balance hard-to-abate residual emissions.

The portfolio of removal approaches spans nature-based solutions and engineered technologies. Nature-based approaches include reforestation, afforestation, soil carbon sequestration, blue carbon (mangroves, seagrasses, salt marshes), and biochar. Engineered approaches include direct air capture (DAC), bioenergy with carbon capture and storage (BECCS), enhanced rock weathering, ocean alkalinity enhancement, and ocean iron fertilization. Each approach has different cost profiles, durability of storage, scalability constraints, and additional environmental and social implications.

Direct air capture, where engineered systems extract CO2 from ambient air using either chemical solvents or solid sorbents, has progressed from research demonstrations to first commercial-scale deployment. Climeworks' Mammoth facility in Iceland, commissioned in 2024, has nameplate capacity of 36,000 tons of CO2 per year — small relative to scale needs but representing a significant scale-up over earlier demonstrations. Several other facilities are under construction or planning, with combined planned capacity of several hundred thousand tons per year by the late 2020s.

The cost of DAC has been a primary barrier. Current costs range from approximately $400 to $1000 per ton of CO2 removed, depending on technology and energy source. The U.S. Inflation Reduction Act's 45Q tax credit provides up to $180 per ton for DAC with permanent geological storage, and an additional credit pathway for utilization, substantially improving the economics in the U.S. The U.S. Department of Energy's Direct Air Capture Hubs program is building four large-scale DAC demonstration projects with combined capacity targeting 1 million tons per year, with first operations expected in the late 2020s.

Bioenergy with carbon capture and storage, or BECCS, involves growing biomass (which absorbs CO2 from the atmosphere as it grows), burning the biomass for energy, and capturing and permanently storing the resulting CO2. The pathway has theoretical appeal because the energy production can offset much of the cost. Practical deployment has been limited, with the largest active BECCS facility being the Drax power station in the U.K. retrofitting CCS to existing biomass-fired generation. Concerns about land use, biodiversity impacts, and the genuine carbon-negativity of large-scale biomass production have constrained rapid scale-up.

Reforestation and afforestation have been the workhorse of the carbon offset market for decades but have generated significant controversy regarding the durability of storage, the additionality of credited projects, and indirect land-use effects. Major scandals in the voluntary carbon market through 2022-2024 substantially undermined confidence in older-vintage forest credits. Newer approaches with stronger monitoring, more conservative crediting, and better attention to additionality are being deployed, but rebuilding market confidence has been slow.

Soil carbon sequestration through changes in agricultural practice — cover cropping, reduced tillage, improved grazing management, and similar techniques — has substantial theoretical potential. Practical implementation has faced challenges in measurement, verification, and the durability of storage. Several major agricultural soil carbon programs in the U.S., E.U., and Australia are deploying remote sensing and modeling approaches to address measurement challenges, with mixed results on cost and credit quality.

Enhanced rock weathering involves spreading finely ground silicate rock on agricultural land or coastlines, where natural chemical weathering reactions absorb CO2 over years to decades. The pathway has substantial theoretical scale potential and is being commercially deployed by a handful of companies. Open scientific questions about realized capture rates under field conditions remain.

Ocean-based approaches include alkalinity enhancement, where mineral additions increase the ocean's capacity to absorb CO2, and iron fertilization, where micronutrient additions stimulate ocean phytoplankton growth and subsequent carbon sequestration. Both approaches have unresolved questions about ecosystem impacts and remain at smaller research scales as of 2025. International governance regimes for ocean climate intervention are emerging through the London Convention/London Protocol process.

Policy frameworks for carbon removal are being developed in parallel with technical scale-up. Voluntary corporate commitments to carbon removal, particularly from technology companies, have been a significant source of early demand. Microsoft's commitment to remove all of its historical emissions by 2050 has driven the largest single corporate procurement of high-quality carbon removal credits to date. Stripe's Frontier program, which aggregates corporate commitments, has financed many early-stage removal projects. Public-sector procurement is emerging through DOE's program and similar efforts in Europe.

Quality standards for removal credits are evolving rapidly. The Carbon Removal Standards Initiative and similar bodies are developing methodology standards that emphasize durability of storage, accurate measurement, and conservative crediting. Removal credits trade at premiums of $100-1000 per ton over typical voluntary market avoidance credits, reflecting both higher quality and the costs of permanent removal versus avoided emissions.

The policy debate around carbon removal includes important moral hazard concerns. Critics argue that promising future removal can be used as rhetoric to justify slower emissions reductions in the present, undermining mitigation ambition. Proponents argue that removal capacity must be developed in parallel with mitigation given the residual emissions in even aggressive decarbonization scenarios. The eventual scale of removal and its place in the broader climate policy portfolio remains contested even as the industrial scale-up proceeds."""
    },
]


_FIXTURES: dict[str, list[dict[str, str]]] = {
    "renewable energy": _RENEWABLE_SOURCES,
    "ai agents": _AI_AGENTS_SOURCES,
    "climate policy": _CLIMATE_POLICY_SOURCES,
}


# ---------------------------------------------------------------------------
# Slugification & lookups
# ---------------------------------------------------------------------------


def slugify(value: str) -> str:
    """Make a filesystem-safe slug out of an arbitrary string."""
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower())
    return cleaned.strip("-") or "topic"


def get_fixture_sources(topic: str) -> list[dict[str, str]]:
    """Return the bundled fixture sources for a topic.

    Falls back to the renewable-energy fixtures for unknown topics so the
    demo always produces a meaningful comparison, even when run with a
    user-specified topic that we don't have canned data for.
    """
    canonical = topic.strip().lower()
    if canonical in _FIXTURES:
        return _FIXTURES[canonical]
    # Best-effort match on substrings for forgiveness.
    for key, sources in _FIXTURES.items():
        if key in canonical or canonical in key:
            return sources
    # Final fallback: synthesize a reasonable shape from the renewable
    # fixtures, retitled for the requested topic.
    base = _FIXTURES["renewable energy"]
    return [
        {
            "url": f"mock://{slugify(topic)}-{i + 1}",
            "title": f"{topic.title()}: {src['title'].split(':', 1)[-1].strip() if ':' in src['title'] else src['title']}",
            "snippet": src["snippet"],
            "content": src["content"],
        }
        for i, src in enumerate(base)
    ]


# ---------------------------------------------------------------------------
# Mock LLM
# ---------------------------------------------------------------------------


def _stable_seed(text: str) -> int:
    """Stable integer seed derived from a string."""
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)


def _last_user_text(history: list[tuple[str, str]]) -> str:
    """Return the most recent user message text, or an empty string."""
    for role, content in reversed(history):
        if role == "user":
            return content
    return ""


_FACT_TEMPLATES = [
    (
        "Key facts:\n"
        "- {a1}\n"
        "- {a2}\n"
        "- {a3}\n"
        "- {a4}\n"
        "- {a5}\n"
    ),
    (
        "Extracted findings:\n"
        "1. {a1}\n"
        "2. {a2}\n"
        "3. {a3}\n"
        "4. {a4}\n"
    ),
    (
        "Summary of source:\n"
        "* {a1}\n"
        "* {a2}\n"
        "* {a3}\n"
    ),
]


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
    """Deterministically pick ``n`` items from ``items`` using ``seed``."""
    chosen: list[Any] = []
    used: set[int] = set()
    for i in range(n):
        idx = (seed + i * 31) % len(items)
        # Ensure no duplicates within a single call
        offset = 0
        while idx in used and offset < len(items):
            offset += 1
            idx = (seed + i * 31 + offset) % len(items)
        used.add(idx)
        chosen.append(items[idx])
    return chosen


def _make_extraction_response(history: list[tuple[str, str]]) -> str:
    """Generate a deterministic ~200-token list of facts."""
    user_text = _last_user_text(history)
    seed = _stable_seed(user_text)
    template = _FACT_TEMPLATES[seed % len(_FACT_TEMPLATES)]
    facts = _pick(seed, _FACT_BANK, 5)
    body = template.format(
        a1=facts[0],
        a2=facts[1],
        a3=facts[2],
        a4=facts[3] if len(facts) > 3 else facts[0],
        a5=facts[4] if len(facts) > 4 else facts[0],
    )
    # Pad with a stable closing comment so the token count is realistic.
    return (
        body
        + "\nThese findings reflect the current consensus as represented "
        "in the source. Confidence: high. Areas of dispute: cost trajectories, "
        "geopolitical exposure, and the pace of regulatory adaptation."
    )


def _make_synthesis_response(history: list[tuple[str, str]], topic: str) -> str:
    """Generate a deterministic ~500-token markdown report."""
    user_text = _last_user_text(history)
    seed = _stable_seed(user_text + topic)
    facts = _pick(seed, _FACT_BANK, 8)
    return f"""# Research report: {topic}

## Executive summary

This report synthesises findings on **{topic}** drawn from five primary sources.
Across the literature, several themes emerge consistently and merit highlighting
for stakeholders weighing investment, policy, or operational decisions in this
space.

## Key findings

1. {facts[0]}.
2. {facts[1]}.
3. {facts[2]}.
4. {facts[3]}.
5. {facts[4]}.

## Detailed analysis

The first observation is that **{facts[0].lower()}**. Multiple sources
corroborate this, with quantitative evidence pointing to the same conclusion.
The implication for practitioners is that strategic planning needs to assume a
materially different cost structure than was prevalent five years ago.

The second observation is that **{facts[1].lower()}**. This concentration
creates both efficiency benefits — economies of scale, learning effects — and
fragility, in that disruptions to the dominant clusters could affect global
supply. Sources differ on the appropriate policy response, with some arguing
for active diversification mandates and others for letting market dynamics
shape the eventual industry structure.

The third observation, that **{facts[2].lower()}**, is closely linked to the
fourth: **{facts[3].lower()}**. Together these point to an environment where
political and regulatory factors are at least as decisive as pure technology
or unit-economics considerations. Forward-looking analysis must therefore
consider scenarios across multiple political environments.

A further observation worth highlighting is **{facts[5].lower()}**, which
serves as a counterweight to the headline narrative of rapid progress.

## Cross-source synthesis

The most striking pattern across the five sources is the consistency of the
direction of change paired with substantial disagreement about pace. All
sources agree that the trajectory of {topic} is one of significant
transformation; they differ on timeline, on which actors will lead, and on
which sub-segments will see the steepest changes.

A second cross-cutting theme is the increasing role of **{facts[6].lower()}**
as a structural feature, which several sources note as a recurring topic that
will shape outcomes through the next decade.

## Sources cited

This synthesis draws on five primary sources covering technology, policy,
economics, market structure, and operational considerations relevant to
{topic}. References available in the workspace under `sources/`.

## Recommendations

For decision-makers, the practical recommendations are: track cost-curve
indicators on a quarterly cadence; build optionality into supply-chain
strategy given the geopolitical concentration risks identified above;
maintain active engagement with policy developments at both subnational and
national levels; and revisit underlying assumptions at regular intervals
given the unusual rate of change in the underlying technologies and policies.

Confidence in these conclusions is moderate-to-high; the most uncertain
elements relate to political durability of current policy commitments and
the realised cost trajectories of pre-commercial technology pathways.
"""


def _make_short_response(history: list[tuple[str, str]]) -> str:
    """Generate a short ack response for fetch-decision steps."""
    return "Acknowledged. Proceeding."


def call_mock_llm(
    history: list[tuple[str, str]],
    *,
    purpose: str = "extraction",
    topic: str = "",
) -> str:
    """Return a deterministic LLM-style response shaped by ``purpose``.

    Args:
        history: List of (role, content) tuples representing the chat
            history. The most recent ``user`` message drives the seed,
            so the same history always yields the same response.
        purpose: One of ``"extraction"``, ``"synthesis"``, or
            ``"short"``. Controls the response template.
        topic: The research topic, used when synthesising the report
            so the output mentions it by name.

    Returns:
        A plausibly-shaped response string. Tokens count realistically.
    """
    if purpose == "synthesis":
        return _make_synthesis_response(history, topic)
    if purpose == "short":
        return _make_short_response(history)
    return _make_extraction_response(history)


def call_anthropic_llm(
    history: list[tuple[str, str]],
    *,
    purpose: str,
    topic: str,
) -> str:
    """Real Anthropic Sonnet call. Used only in ``--mode=live``.

    The function is imported lazily so the demo dependency on the
    ``anthropic`` package is genuinely optional.
    """
    try:
        from anthropic import Anthropic  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised manually
        raise SystemExit(
            "Live mode requires the anthropic package. Install with: "
            "pip install 'plinth-example-research-agent[live]'"
        ) from exc

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit(
            "Live mode requires ANTHROPIC_API_KEY env var. "
            "Falling back to simulation mode by default."
        )

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
        "synthesis": f"Synthesise a 500-1000 word markdown report on '{topic}'.",
        "short": "Respond briefly with an acknowledgement.",
    }.get(purpose, "Respond helpfully.")

    response = client.messages.create(
        model="claude-3-5-sonnet-latest",
        max_tokens=2000 if purpose == "synthesis" else 800,
        system=system,
        messages=messages,
    )
    return "".join(
        block.text for block in response.content if getattr(block, "type", "") == "text"
    )


# ---------------------------------------------------------------------------
# Tool-call layer (baseline = direct, plinth = gateway)
# ---------------------------------------------------------------------------


@dataclass
class ToolCallResult:
    """Result of a tool call, with bookkeeping for the demo."""

    tool: str
    arguments: dict[str, Any]
    result: dict[str, Any]
    cached: bool = False
    duration_ms: int = 0


class FixtureToolBackend:
    """In-process backend that serves the bundled fixtures.

    Used in simulation mode so the demo runs without any services.
    Both baseline and Plinth modes use this; the difference is whether
    they go through a caching wrapper (Plinth) or not (baseline).
    """

    def __init__(self) -> None:
        self._call_count = 0

    def search(self, query: str, k: int = 5) -> dict[str, Any]:
        sources = get_fixture_sources(query)[:k]
        self._call_count += 1
        return {
            "results": [
                {
                    "url": s["url"],
                    "title": s["title"],
                    "snippet": s["snippet"],
                }
                for s in sources
            ]
        }

    def fetch(self, url: str) -> dict[str, Any]:
        # Find by URL across all fixtures.
        self._call_count += 1
        for sources in _FIXTURES.values():
            for src in sources:
                if src["url"] == url:
                    return {
                        "content": src["content"],
                        "status": 200,
                        "content_type": "text/markdown",
                    }
        # Synthesized URLs for unknown topics: ``mock://<topic-slug>-<idx>``.
        # We map the trailing index to the renewable-energy fixture content
        # so the comparison still produces realistic per-source token sizes.
        match = re.match(r"mock://.+-(\d+)$", url)
        if match:
            idx = int(match.group(1)) - 1
            base = _FIXTURES["renewable energy"]
            if 0 <= idx < len(base):
                return {
                    "content": base[idx]["content"],
                    "status": 200,
                    "content_type": "text/markdown",
                }
        # Last resort: return a small synthetic body so the demo still runs.
        return {
            "content": (
                f"[mock] Source content for {url}. "
                "This is a fallback fixture for the research-agent demo."
            ),
            "status": 200,
            "content_type": "text/plain",
        }

    @property
    def call_count(self) -> int:
        return self._call_count


class HTTPToolBackend:
    """Backend that calls the mock-mcp service over HTTP at :7423.

    Activated when callers can reach the mock-mcp server. Used by both
    baseline (direct) and Plinth (when the gateway proxies to mock-mcp)
    when running with services up.
    """

    def __init__(self, base_url: str = MOCK_MCP_URL) -> None:
        self._base_url = base_url.rstrip("/")
        self._call_count = 0

    def search(self, query: str, k: int = 5) -> dict[str, Any]:
        self._call_count += 1
        with httpx.Client(timeout=10.0) as client:
            r = client.post(
                f"{self._base_url}/invoke/web.search",
                json={"query": query, "k": k},
            )
            r.raise_for_status()
            payload = r.json()
            # mock-mcp wraps responses in {"result": ...}; unwrap to match
            # the FixtureToolBackend's flat shape.
            return payload.get("result", payload)

    def fetch(self, url: str) -> dict[str, Any]:
        self._call_count += 1
        with httpx.Client(timeout=15.0) as client:
            r = client.post(
                f"{self._base_url}/invoke/web.fetch",
                json={"url": url},
            )
            r.raise_for_status()
            payload = r.json()
            return payload.get("result", payload)

    @property
    def call_count(self) -> int:
        return self._call_count


def get_tool_backend() -> tuple[Any, str]:
    """Pick a backend: HTTP if mock-mcp is reachable, else fixtures.

    Returns:
        ``(backend, mode)`` where mode is either ``"http"`` or
        ``"fixture"``.
    """
    try:
        with httpx.Client(timeout=1.0) as client:
            client.get(f"{MOCK_MCP_URL}/healthz")
        return HTTPToolBackend(), "http"
    except Exception:
        return FixtureToolBackend(), "fixture"


# ---------------------------------------------------------------------------
# History-as-prompt helpers
# ---------------------------------------------------------------------------


def history_to_prompt(history: list[tuple[str, str]]) -> str:
    """Flatten chat history into one text blob for tokenisation.

    The exact wire format for production LLMs has structural overhead
    that this simple join undercounts slightly. We accept that — the
    point of the comparison is the *ratio* between baseline and Plinth,
    and that ratio is robust to small per-call overhead.
    """
    parts = []
    for role, content in history:
        parts.append(f"<{role}>\n{content}\n</{role}>")
    return "\n".join(parts)


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
class ToolCallRecord:
    """One tool invocation, with caching info."""

    tool: str
    arguments: dict[str, Any]
    cached: bool
    duration_ms: int


@dataclass
class ResearchReport:
    """Complete output of a research run."""

    topic: str
    report_text: str
    sources: list[dict[str, Any]]
    llm_calls: list[LLMCallRecord] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    wall_clock_seconds: float = 0.0

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
    def llm_call_count(self) -> int:
        return len(self.llm_calls)

    @property
    def tool_call_count(self) -> int:
        return len(self.tool_calls)

    @property
    def cached_tool_calls(self) -> int:
        return sum(1 for c in self.tool_calls if c.cached)

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "report_text": self.report_text,
            "sources": self.sources,
            "summary": {
                "total_input_tokens": self.total_input_tokens,
                "total_output_tokens": self.total_output_tokens,
                "total_tokens": self.total_tokens,
                "total_cost_usd": self.total_cost_usd,
                "llm_calls": self.llm_call_count,
                "tool_calls": self.tool_call_count,
                "cached_tool_calls": self.cached_tool_calls,
                "wall_clock_seconds": self.wall_clock_seconds,
            },
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
        }


# ---------------------------------------------------------------------------
# LLM dispatch with token accounting
# ---------------------------------------------------------------------------


def llm_call(
    history: list[tuple[str, str]],
    *,
    step: str,
    purpose: str,
    topic: str,
    mode: str,
    record: ResearchReport,
) -> str:
    """Call the LLM (mock or live), record tokens, and return the text.

    This is the single place that token accounting happens. Both
    baseline and Plinth go through it, so the only thing that affects
    the comparison is what each one *puts* in the history.
    """
    prompt = history_to_prompt(history)
    prompt_tokens = count_tokens(prompt)
    start = time.perf_counter()
    if mode == "live":
        try:
            response = call_anthropic_llm(history, purpose=purpose, topic=topic)
        except SystemExit as e:
            print(f"[live mode unavailable] {e}; falling back to simulation")
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
# Plinth-services availability detection (used by with_plinth.py)
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
# Topics file
# ---------------------------------------------------------------------------


def load_topics_config() -> dict[str, Any]:
    """Read ``topics.json`` from the example directory."""
    path = os.path.join(os.path.dirname(__file__), "topics.json")
    with open(path) as f:
        return json.load(f)


__all__ = [
    "FixtureToolBackend",
    "HTTPToolBackend",
    "LLMCallRecord",
    "ResearchReport",
    "ToolCallRecord",
    "call_anthropic_llm",
    "call_mock_llm",
    "count_tokens",
    "estimate_cost",
    "get_fixture_sources",
    "get_tool_backend",
    "history_to_prompt",
    "llm_call",
    "load_topics_config",
    "services_available",
    "slugify",
]
