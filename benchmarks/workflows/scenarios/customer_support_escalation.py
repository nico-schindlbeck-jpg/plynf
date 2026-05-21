"""Customer support agent — 10-turn ticket handling with context lookup.

Persona: Customer support team running a tier-1 automation. Agent
handles a 10-turn conversation with a customer who has a complex
issue. At several turns, the agent fetches: prior tickets from the
customer, relevant KB articles, the customer's CRM record, current
product version notes.

This is the workflow that hurts most in chat-history-style agents:
every customer-message turn re-sends the entire conversation PLUS all
the context fetched along the way.
"""

from __future__ import annotations

from ..harness import Scenario, Step, StepKind

CUSTOMER_MSG_TOKENS = 250
AGENT_REPLY_TOKENS = 400
CRM_RECORD_TOKENS = 2_500
PRIOR_TICKET_TOKENS = 3_000
KB_ARTICLE_TOKENS = 4_500
PRODUCT_NOTES_TOKENS = 5_000

# Turn structure: customer says something, agent (maybe) looks up
# context, agent replies. Over 10 turns the agent does 4 context fetches.

SCENARIO = Scenario(
    id="customer-support-escalation",
    title="10-turn customer support with 4 lookups",
    persona="Customer support / tier-1 automation",
    description=(
        "10-turn conversation with a customer. Agent does 4 context "
        "lookups along the way (CRM record, 2 prior tickets, 1 KB "
        "article, product notes). Modelling realistic mid-complexity "
        "ticket: integration bug requiring history."
    ),
    steps=(
        # ─── Turn 1 ──────────────────────────────────────────────────
        Step(
            id="t1_customer",
            kind=StepKind.TOOL_CALL,
            produces_tokens=CUSTOMER_MSG_TOKENS,
            prompt_overhead=50,
            requires=(),
        ),
        Step(
            id="t1_lookup_crm",
            kind=StepKind.TOOL_CALL,
            produces_tokens=CRM_RECORD_TOKENS,
            prompt_overhead=80,
            requires=("t1_customer",),
        ),
        Step(
            id="t1_reply",
            kind=StepKind.MODEL_CALL,
            produces_tokens=AGENT_REPLY_TOKENS,
            prompt_overhead=300,
            requires=("t1_customer", "t1_lookup_crm"),
            read_mode="slice",
        ),
        # ─── Turn 2 ──────────────────────────────────────────────────
        Step(
            id="t2_customer",
            kind=StepKind.TOOL_CALL,
            produces_tokens=CUSTOMER_MSG_TOKENS,
            prompt_overhead=50,
            requires=("t1_reply",),
        ),
        Step(
            id="t2_lookup_ticket_1",
            kind=StepKind.TOOL_CALL,
            produces_tokens=PRIOR_TICKET_TOKENS,
            prompt_overhead=80,
            requires=("t2_customer",),
        ),
        Step(
            id="t2_reply",
            kind=StepKind.MODEL_CALL,
            produces_tokens=AGENT_REPLY_TOKENS,
            prompt_overhead=300,
            requires=("t2_customer", "t1_lookup_crm", "t2_lookup_ticket_1"),
            read_mode="summary",  # reply needs gists, not full content
        ),
        # ─── Turn 3 ──────────────────────────────────────────────────
        Step(
            id="t3_customer",
            kind=StepKind.TOOL_CALL,
            produces_tokens=CUSTOMER_MSG_TOKENS,
            prompt_overhead=50,
            requires=("t2_reply",),
        ),
        Step(
            id="t3_reply",
            kind=StepKind.MODEL_CALL,
            produces_tokens=AGENT_REPLY_TOKENS,
            prompt_overhead=300,
            requires=("t3_customer", "t2_lookup_ticket_1"),
            read_mode="summary",
        ),
        # ─── Turn 4 ──────────────────────────────────────────────────
        Step(
            id="t4_customer",
            kind=StepKind.TOOL_CALL,
            produces_tokens=CUSTOMER_MSG_TOKENS,
            prompt_overhead=50,
            requires=("t3_reply",),
        ),
        Step(
            id="t4_lookup_kb",
            kind=StepKind.TOOL_CALL,
            produces_tokens=KB_ARTICLE_TOKENS,
            prompt_overhead=80,
            requires=("t4_customer",),
        ),
        Step(
            id="t4_reply",
            kind=StepKind.MODEL_CALL,
            produces_tokens=AGENT_REPLY_TOKENS,
            prompt_overhead=300,
            requires=("t4_customer", "t4_lookup_kb"),
            read_mode="slice",
        ),
        # ─── Turns 5-7 (no new lookups, just back-and-forth) ────────
        *[
            step
            for turn in range(5, 8)
            for step in (
                Step(
                    id=f"t{turn}_customer",
                    kind=StepKind.TOOL_CALL,
                    produces_tokens=CUSTOMER_MSG_TOKENS,
                    prompt_overhead=50,
                    requires=(f"t{turn - 1}_reply",),
                ),
                Step(
                    id=f"t{turn}_reply",
                    kind=StepKind.MODEL_CALL,
                    produces_tokens=AGENT_REPLY_TOKENS,
                    prompt_overhead=300,
                    requires=(
                        f"t{turn}_customer",
                        "t1_lookup_crm",
                        "t4_lookup_kb",
                    ),
                    read_mode="summary",
                ),
            )
        ],
        # ─── Turn 8: another lookup ─────────────────────────────────
        Step(
            id="t8_customer",
            kind=StepKind.TOOL_CALL,
            produces_tokens=CUSTOMER_MSG_TOKENS,
            prompt_overhead=50,
            requires=("t7_reply",),
        ),
        Step(
            id="t8_lookup_ticket_2",
            kind=StepKind.TOOL_CALL,
            produces_tokens=PRIOR_TICKET_TOKENS,
            prompt_overhead=80,
            requires=("t8_customer",),
        ),
        Step(
            id="t8_reply",
            kind=StepKind.MODEL_CALL,
            produces_tokens=AGENT_REPLY_TOKENS,
            prompt_overhead=300,
            requires=("t8_customer", "t8_lookup_ticket_2"),
            read_mode="slice",
        ),
        # ─── Turns 9-10: wrap-up ────────────────────────────────────
        *[
            step
            for turn in range(9, 11)
            for step in (
                Step(
                    id=f"t{turn}_customer",
                    kind=StepKind.TOOL_CALL,
                    produces_tokens=CUSTOMER_MSG_TOKENS,
                    prompt_overhead=50,
                    requires=(f"t{turn - 1}_reply",),
                ),
                Step(
                    id=f"t{turn}_reply",
                    kind=StepKind.MODEL_CALL,
                    produces_tokens=AGENT_REPLY_TOKENS,
                    prompt_overhead=300,
                    requires=(
                        f"t{turn}_customer",
                        "t1_lookup_crm",
                        "t4_lookup_kb",
                        "t8_lookup_ticket_2",
                    ),
                    read_mode="summary",
                ),
            )
        ],
        # ─── Closing summary written for the CRM ───────────────────
        Step(
            id="ticket_resolution_note",
            kind=StepKind.MODEL_CALL,
            produces_tokens=700,
            prompt_overhead=250,
            requires=("t1_lookup_crm", "t10_reply"),
            read_mode="summary",
        ),
    ),
    notes=(
        "Support workflows are where chat-history models bleed money. "
        "By turn 10, baseline has accumulated ~50k tokens of context "
        "that gets re-sent on every reply.",
        "Plinth's per-turn reply only carries handles to the lookups "
        "that turn actually needs. Customer messages don't even need "
        "handles — they're just part of the current turn's prompt.",
        "Modelled with a single static CRM lookup; production workflows "
        "often re-fetch CRM data if it stales — Plinth caches the "
        "handle and the gateway returns 304 Not Modified, near-zero cost.",
        "Exceeds 85% sanity floor — extreme reduction is the structural "
        "characteristic of multi-turn workflows. Every additional turn "
        "in baseline carries the full prior accumulation; this is "
        "quadratic-vs-linear made concrete. 10-turn workflows are not "
        "an outlier — many production support flows hit 20+ turns.",
    ),
)
