# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Plynf LLM-Proxy — Agent Context Optimization Layer.

OpenAI-compatible HTTP proxy that intercepts tool calls between an AI agent
and the tools it queries (Salesforce, Slack, Order-DB, ...). Tool responses
are reshaped by a declarative policy engine *before* they enter the LLM's
context window — reducing input tokens, latency, and data exposure.
"""

__version__ = "0.1.0"
