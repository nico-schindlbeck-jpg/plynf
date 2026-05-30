# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Per-request upstream routing for OpenAI-compatible front doors.

By default Plynf forwards every OpenAI-shaped request to a single upstream
(``PLINTH_PROXY_UPSTREAM_BASE_URL``). That ties one proxy instance to one
provider. This module lets a single instance front *any* number of
OpenAI-compatible providers — OpenAI, Azure, Groq, Together, Mistral, DeepSeek,
xAI, Fireworks, OpenRouter, Perplexity, a local vLLM/Ollama, ... — chosen per
request, **without gating on which provider**. Plynf gates on volume and
features, never on integration type, so multi-provider routing is available to
every tier.

Resolution order for a request:

  1. An explicit ``X-Plynf-Upstream`` header (optionally with
     ``X-Plynf-Upstream-Key``) — an escape hatch for ad-hoc base URLs. When the
     key header is omitted the configured default key is reused, so a caller can
     override only the URL.
  2. A ``provider/model`` prefix on the model string (the LiteLLM / OpenRouter
     convention), matched against the configured providers. The prefix is
     stripped before forwarding so the upstream sees its native model id
     (``groq/llama-3.3-70b`` → base ``https://api.groq.com/openai`` + model
     ``llama-3.3-70b``). Only the first path segment is consumed, so nested ids
     such as ``openrouter/anthropic/claude-3.5`` keep their remainder intact.
  3. The default upstream (``upstream_base_url`` / ``upstream_api_key``).

When no providers are configured and no header/prefix is present, resolution
returns the default upstream **unchanged** — existing single-provider
deployments behave exactly as before.

Provider config (``PLINTH_PROXY_PROVIDERS``) is a JSON array of objects, each
``{"name", "base_url", "api_key"}``::

    [
      {"name": "groq", "base_url": "https://api.groq.com/openai", "api_key": "${GROQ_API_KEY}"},
      {"name": "mistral", "base_url": "https://api.mistral.ai", "api_key": "${MISTRAL_API_KEY}"}
    ]

``${VAR}`` references in ``base_url`` and ``api_key`` are expanded from the
environment at construction time (unset → empty string), so secrets stay out of
the config blob.

Optionally, ``PLINTH_PROXY_MODEL_ALIASES`` maps friendly names to concrete model
strings (``{"fast": "groq/llama-3.1-8b-instant"}``). An alias is expanded once,
before routing, so a team can name models in one place and swap the provider
behind them without touching application code.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

__all__ = [
    "ProviderRoute",
    "UpstreamTarget",
    "UpstreamRouter",
    "parse_providers",
    "parse_aliases",
]

# Header names callers use to override the upstream per request.
HEADER_BASE_URL = "x-plynf-upstream"
HEADER_API_KEY = "x-plynf-upstream-key"

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(value: str) -> str:
    """Expand ``${VAR}`` references from the environment (unset → empty)."""
    return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)


@dataclass(frozen=True)
class ProviderRoute:
    """A configured OpenAI-compatible upstream addressable by name prefix."""

    name: str
    base_url: str
    api_key: str = ""


@dataclass(frozen=True)
class UpstreamTarget:
    """A resolved upstream: where to send, which key, and the model to send.

    ``provider`` is ``"default"`` for the configured fallback upstream,
    ``"header"`` for a per-request header override, or the configured provider
    name when a ``provider/model`` prefix matched. ``model`` is the id the
    upstream should see — i.e. with any matched provider prefix stripped.
    """

    base_url: str
    api_key: str
    model: str
    provider: str

    @property
    def is_real(self) -> bool:
        """True when there is somewhere to actually forward to."""
        return bool(self.base_url)

    def chat_completions_url(self) -> str:
        """Convenience: the ``/v1/chat/completions`` URL for this target."""
        return self.url("/v1/chat/completions")

    def url(self, path: str) -> str:
        """Join this target's base URL with ``path`` (which must start ``/``)."""
        return self.base_url.rstrip("/") + path


def parse_providers(raw: str) -> list[ProviderRoute]:
    """Parse ``PLINTH_PROXY_PROVIDERS`` JSON into :class:`ProviderRoute` objects.

    Empty / whitespace input yields an empty list. Malformed JSON or a non-array
    payload raises ``ValueError`` with a clear message (callers that must not
    crash on bad config should catch it). Entries missing ``name`` or
    ``base_url`` are skipped. ``${VAR}`` references in ``base_url`` / ``api_key``
    are expanded from the environment.
    """
    if not raw or not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"PLINTH_PROXY_PROVIDERS is not valid JSON: {e}") from e
    if not isinstance(data, list):
        raise ValueError("PLINTH_PROXY_PROVIDERS must be a JSON array of objects")

    out: list[ProviderRoute] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        base_url = _expand_env(str(item.get("base_url", "")).strip())
        if not name or not base_url:
            continue
        if name in seen:
            continue  # first definition wins; ignore later duplicates
        seen.add(name)
        api_key = _expand_env(str(item.get("api_key", "")))
        out.append(ProviderRoute(name=name, base_url=base_url.rstrip("/"), api_key=api_key))
    return out


def parse_aliases(raw: str) -> dict[str, str]:
    """Parse ``PLINTH_PROXY_MODEL_ALIASES`` JSON into an ``{alias: target}`` map.

    Empty / whitespace input yields an empty dict. Malformed JSON or a non-object
    payload raises ``ValueError``. Aliases let a team name models once
    (``"fast"`` → ``"groq/llama-3.1-8b-instant"``) and switch the provider behind
    them via config, with no application code change. Targets may themselves
    carry a ``provider/model`` prefix; resolution is single-level (an alias whose
    target is another alias is not expanded recursively).
    """
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"PLINTH_PROXY_MODEL_ALIASES is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("PLINTH_PROXY_MODEL_ALIASES must be a JSON object")
    out: dict[str, str] = {}
    for k, v in data.items():
        alias, target = str(k).strip(), str(v).strip()
        if alias and target:
            out[alias] = target
    return out


class UpstreamRouter:
    """Resolves each request to a concrete :class:`UpstreamTarget`."""

    def __init__(
        self,
        providers: list[ProviderRoute] | None = None,
        *,
        default_base_url: str = "",
        default_api_key: str = "",
        aliases: dict[str, str] | None = None,
    ) -> None:
        self._providers: dict[str, ProviderRoute] = {p.name: p for p in (providers or [])}
        self._default_base = default_base_url.rstrip("/") if default_base_url else ""
        self._default_key = default_api_key
        self._aliases: dict[str, str] = dict(aliases or {})

    @classmethod
    def from_settings(cls, settings: object) -> UpstreamRouter:
        """Build a router from :class:`ProxySettings`.

        Bad provider config never crashes startup: a parse error degrades to a
        default-only router (mirroring how custom REST connectors are handled).
        The caller is expected to log; we stay silent here to keep this module
        free of logging policy.
        """
        try:
            providers = parse_providers(getattr(settings, "providers", "") or "")
        except ValueError:
            providers = []
        try:
            aliases = parse_aliases(getattr(settings, "model_aliases", "") or "")
        except ValueError:
            aliases = {}
        return cls(
            providers,
            default_base_url=getattr(settings, "upstream_base_url", "") or "",
            default_api_key=getattr(settings, "upstream_api_key", "") or "",
            aliases=aliases,
        )

    @property
    def provider_names(self) -> list[str]:
        """Sorted names of the configured providers (for ``/v1/connectors`` etc.)."""
        return sorted(self._providers)

    @property
    def alias_names(self) -> list[str]:
        """Sorted alias names (for discoverability; never exposes targets)."""
        return sorted(self._aliases)

    @property
    def has_default(self) -> bool:
        return bool(self._default_base)

    def resolve(
        self,
        model: str,
        *,
        header_base_url: str | None = None,
        header_api_key: str | None = None,
    ) -> UpstreamTarget:
        """Resolve ``model`` (+ optional header overrides) to an upstream target.

        See the module docstring for the resolution order. ``model`` is returned
        with the provider prefix stripped only when that prefix matched a
        configured provider; otherwise it is passed through verbatim so unrelated
        slashes (``meta-llama/Llama-3``) are never mangled.

        A configured alias is expanded first (single level), so the rest of the
        resolution — header / prefix / default — sees the alias target.
        """
        model = model or ""
        # 0. Alias expansion (single level): "fast" -> "groq/llama-3.1-8b".
        model = self._aliases.get(model, model)

        # 1. Explicit header override.
        if header_base_url and header_base_url.strip():
            key = header_api_key if header_api_key is not None else self._default_key
            return UpstreamTarget(
                base_url=header_base_url.strip().rstrip("/"),
                api_key=key,
                model=model,
                provider="header",
            )

        # 2. provider/model prefix.
        if "/" in model:
            prefix, _, rest = model.partition("/")
            route = self._providers.get(prefix)
            if route is not None and rest:
                return UpstreamTarget(
                    base_url=route.base_url,
                    api_key=route.api_key,
                    model=rest,
                    provider=route.name,
                )

        # 3. Default upstream (unchanged).
        return UpstreamTarget(
            base_url=self._default_base,
            api_key=self._default_key,
            model=model,
            provider="default",
        )
