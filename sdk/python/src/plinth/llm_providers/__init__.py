# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Built-in LLM providers for the Plinth SDK.

Each provider is a small adapter wrapping a vendor SDK behind the
:class:`plinth.llm.LLMProvider` protocol. They are imported lazily from
:func:`build_provider` so the optional pip extras (``plinth[anthropic]``,
``plinth[openai]``) only get pulled in when actually used.

Adding a new provider is intentionally a single file change:

1. Add ``llm_providers/<name>.py`` with a ``Provider`` class implementing
   the protocol.
2. Wire it into :func:`build_provider` below.
3. Optionally add a pricing dict for the cost helper.

The :class:`MockProvider` ships unconditionally — tests rely on it.
"""

from __future__ import annotations

from typing import Any

from .mock import MockProvider


def build_provider(name: str, **config: Any) -> Any:
    """Construct the named built-in provider.

    Raised :class:`~plinth.exceptions.LLMProviderNotInstalled` mentions
    the exact pip extra the user is missing — keeps the diagnostic close
    to the failure.
    """
    from ..exceptions import LLMProviderNotInstalled

    name_lower = name.lower()
    if name_lower == "mock":
        return MockProvider(**config)
    if name_lower == "anthropic":
        try:
            from .anthropic import AnthropicProvider
        except ImportError as exc:
            raise LLMProviderNotInstalled(
                "The 'anthropic' provider requires the optional dependency. "
                "Install it with: pip install 'plinth[anthropic]'"
            ) from exc
        return AnthropicProvider(**config)
    if name_lower == "openai":
        try:
            from .openai import OpenAIProvider
        except ImportError as exc:
            raise LLMProviderNotInstalled(
                "The 'openai' provider requires the optional dependency. "
                "Install it with: pip install 'plinth[openai]'"
            ) from exc
        return OpenAIProvider(**config)
    raise ValueError(
        f"Unknown LLM provider {name!r}. Built-ins: 'anthropic', 'openai', 'mock'."
    )


__all__ = ["MockProvider", "build_provider"]
