# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Plynf proxy client SDK.

Two ways to use it in a Python agent:

1. **Drop-in OpenAI replacement**::

       from plinth.proxy_client import OpenAI

       client = OpenAI(api_key="sk-…", plynf_url="https://app.plynf.com")
       # client.chat.completions.create(...) — same call shape as `openai`

2. **Wrap your own tools** (LangChain / CrewAI / AutoGen / custom)::

       from plinth.proxy_client import wrap_tools

       my_tools = [salesforce_get_lead, slack_send_msg]
       wrapped = wrap_tools(my_tools, plynf_url="https://app.plynf.com")

In both modes the SDK routes tool calls through Plynf so the response is
shaped before it re-enters the LLM context, and a savings event is logged.
"""

from .openai_drop_in import OpenAI
from .tools_wrap import wrap_tool, wrap_tools


# LangChain-native helpers are loaded lazily — they import langchain_core
# only when the user calls into the helper, so the rest of the SDK stays
# small for non-LangChain users.
def __getattr__(name: str):  # noqa: D401 — module-level dunder
    if name in {"make_plynf_tool", "wrap_langchain_tools"}:
        from . import langchain as _lc

        return getattr(_lc, name)
    raise AttributeError(name)


__all__ = [
    "OpenAI",
    "wrap_tool",
    "wrap_tools",
    "make_plynf_tool",
    "wrap_langchain_tools",
]
