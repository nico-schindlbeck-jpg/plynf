"""Sales-lead enrichment — fetch from 5 sources, write profile, sync to CRM.

Persona: Sales operations team running automated lead-research on
inbound demo requests. Given a lead name + company, the agent:
  1. Looks up the company on the web (company site, news mentions)
  2. Looks up the person (LinkedIn-like profile via tool)
  3. Pulls existing CRM history if any
  4. Pulls funding/employee data (Crunchbase-equivalent)
  5. Synthesises a 1-page profile
  6. Writes structured fields back to the CRM
"""

from __future__ import annotations

from ..harness import Scenario, Step, StepKind

COMPANY_SITE_TOKENS = 6_000
NEWS_MENTIONS_TOKENS = 8_000  # 5 article snippets
PROFILE_TOKENS = 3_500
CRM_HISTORY_TOKENS = 4_000  # existing contacts at the company
FUNDING_DATA_TOKENS = 1_500
SUMMARY_PROFILE_TOKENS = 1_800
CRM_WRITE_TOKENS = 600

SCENARIO = Scenario(
    id="sales-lead-enrichment",
    title="Sales lead enrichment + CRM sync",
    persona="Sales ops / lead research",
    description=(
        "Given lead name + company, fetch from 5 sources (company site, "
        "news, profile lookup, existing CRM, funding data), synthesise "
        "a 1-page profile, sync structured fields to CRM."
    ),
    steps=(
        Step(
            id="fetch_company_site",
            kind=StepKind.TOOL_CALL,
            produces_tokens=COMPANY_SITE_TOKENS,
            prompt_overhead=80,
            requires=(),
        ),
        Step(
            id="fetch_news",
            kind=StepKind.TOOL_CALL,
            produces_tokens=NEWS_MENTIONS_TOKENS,
            prompt_overhead=80,
            requires=(),
        ),
        Step(
            id="fetch_profile",
            kind=StepKind.TOOL_CALL,
            produces_tokens=PROFILE_TOKENS,
            prompt_overhead=80,
            requires=(),
        ),
        Step(
            id="fetch_crm_history",
            kind=StepKind.TOOL_CALL,
            produces_tokens=CRM_HISTORY_TOKENS,
            prompt_overhead=80,
            requires=(),
        ),
        Step(
            id="fetch_funding",
            kind=StepKind.TOOL_CALL,
            produces_tokens=FUNDING_DATA_TOKENS,
            prompt_overhead=80,
            requires=(),
        ),
        Step(
            id="synthesise_profile",
            kind=StepKind.MODEL_CALL,
            produces_tokens=SUMMARY_PROFILE_TOKENS,
            prompt_overhead=350,
            requires=(
                "fetch_company_site",
                "fetch_news",
                "fetch_profile",
                "fetch_crm_history",
                "fetch_funding",
            ),
            read_mode="slice",
        ),
        Step(
            id="extract_crm_fields",
            kind=StepKind.MODEL_CALL,
            produces_tokens=CRM_WRITE_TOKENS,
            prompt_overhead=200,
            requires=("synthesise_profile",),
            read_mode="slice",
        ),
        Step(
            id="crm_write",
            kind=StepKind.TOOL_CALL,
            produces_tokens=200,
            prompt_overhead=120,
            requires=("extract_crm_fields",),
            read_mode="slice",
        ),
    ),
    notes=(
        "Sales-ops workflows are often run in bulk (200 leads/day). "
        "The per-lead savings compound massively: 200 × $0.05 saved = "
        "$3,000/month per sales-ops user.",
        "Real implementations often add 2-3 more fetch sources "
        "(social signals, employee count graphs). The scenario "
        "models a conservative 5-source case.",
    ),
)
