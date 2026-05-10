# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Headline driver for the v1.2 LLM-research-agent demo.

This is a thin wrapper over :mod:`agent` that runs the agent once and
prints a small summary. Mirrors the entry point name in
``examples/01-research-agent/compare.py`` so the spec invocation
``python examples/06-llm-research-agent/compare.py --topic "..."``
keeps working.

Run with no flags to see the MockProvider path. Pass ``--mode=live`` to
exercise the real Anthropic provider (requires ``ANTHROPIC_API_KEY``).
"""

from __future__ import annotations

import sys

from agent import main  # type: ignore[import-untyped]

if __name__ == "__main__":
    sys.exit(main())
