"""Workflow benchmark scenarios — 8 agent workloads with token math.

Each scenario is a Python module that exports a `SCENARIO` constant of
type `Scenario` (see harness.py). The module's docstring documents the
real-world workflow it models and the assumptions it makes.
"""

from __future__ import annotations

from .research_5_source import SCENARIO as research_5_source
from .research_30_pdf import SCENARIO as research_30_pdf
from .multi_agent_content import SCENARIO as multi_agent_content
from .code_review_pr import SCENARIO as code_review_pr
from .customer_support_escalation import SCENARIO as customer_support_escalation
from .sales_lead_enrichment import SCENARIO as sales_lead_enrichment
from .doc_qa_50_contracts import SCENARIO as doc_qa_50_contracts
from .report_writing import SCENARIO as report_writing

ALL_SCENARIOS = [
    research_5_source,
    research_30_pdf,
    multi_agent_content,
    code_review_pr,
    customer_support_escalation,
    sales_lead_enrichment,
    doc_qa_50_contracts,
    report_writing,
]
