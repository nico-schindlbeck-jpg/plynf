# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Plynf proxy settings (Pydantic Settings)."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class ProxySettings(BaseSettings):
    """Runtime config for the LLM proxy.

    All keys use the ``PLINTH_PROXY_`` prefix.
    """

    model_config = SettingsConfigDict(
        env_prefix="PLINTH_PROXY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = 7430

    # If empty, the proxy runs in MOCK mode: incoming /v1/chat/completions
    # gets a deterministic mock LLM response without calling upstream OpenAI.
    # Set to "https://api.openai.com" to forward to real OpenAI.
    upstream_base_url: str = ""

    # Forwarded as-is in MOCK mode (the demo expects a key but doesn't validate);
    # in real-upstream mode, this is the OpenAI API key. Clients may also send
    # their own key via Authorization header — that header takes precedence.
    upstream_api_key: str = ""

    # API keys accepted on the proxy itself. Comma-separated list of
    # ``tenant_id:key`` pairs (no spaces). Empty list = open (demo mode).
    # Optional third field selects the tier: ``tenant_id:key:tier``
    # where tier is one of free/pro/enterprise (default: free).
    api_keys: str = ""

    # Default tier used when no key matches (open / demo mode).
    demo_tier: str = "enterprise"

    # Identity-service URL. When set, the proxy verifies bearer tokens by
    # POSTing them to ``<identity_url>/v1/tokens/verify`` and reads the
    # tenant_id / tier (from ``tier:*`` scopes) from the response.
    # The static api_keys map is checked first as a fallback so local demo
    # setups keep working without running the identity service.
    identity_url: str = ""

    # How long to cache verify responses (seconds). Bounded by token exp.
    identity_cache_ttl_s: int = 60

    # Postgres DSN for the savings sink. Empty = no Postgres, falls back to
    # the JSONL sink (or no persistence at all in demo mode).
    postgres_url: str = ""

    # Hard token budget for the input messages array, applied between tool-
    # call rounds. Set to 0 to disable. When exceeded, the oldest tool
    # responses are replaced with short summary placeholders so the LLM's
    # tool_call_id references still resolve.
    context_budget_input_tokens: int = 0

    # How many most-recent tool messages are protected from rotation.
    context_budget_keep_recent_tool_messages: int = 3

    # Path for the JSON file backing per-tenant policy overrides. Empty
    # disables persistence (overrides live in memory only).
    policy_overrides_path: str = ""

    # Where the policy YAMLs live. Defaults to the packaged set.
    policies_dir: str = ""

    # Where to write JSONL savings events. Empty = no persistence.
    savings_log: str = ""

    # Default model used for cost estimation when the request model is unknown.
    default_model: str = "gpt-4o"

    # Demo: include a mock LLM that pretends to call tools so we can show the
    # full pipeline without an upstream key.
    demo_mode: bool = True

    # MCP-Gateway URL. When set, tool calls are forwarded to the existing
    # plinth-gateway service instead of mock connectors. Demo mode keeps
    # mocks regardless of this setting so the offline demo still works.
    gateway_url: str = ""

    # Optional service-account bearer token used when forwarding to the
    # gateway. Per-request caller tokens override this when present.
    gateway_service_token: str = ""

    @property
    def policies_path(self) -> Path:
        if self.policies_dir:
            return Path(self.policies_dir)
        # Default: the ``policies`` directory packaged with this module.
        return Path(__file__).resolve().parent / "policies"

    def parsed_api_keys(self) -> dict[str, str]:
        """Return ``{api_key: tenant_id}`` map from the comma-separated env."""
        out: dict[str, str] = {}
        if not self.api_keys:
            return out
        for entry in self.api_keys.split(","):
            entry = entry.strip()
            if not entry or ":" not in entry:
                continue
            parts = entry.split(":")
            if len(parts) < 2:
                continue
            tenant_id, key = parts[0].strip(), parts[1].strip()
            out[key] = tenant_id
        return out

    def parsed_api_key_tiers(self) -> dict[str, str]:
        """Return ``{api_key: tier}`` map (third field of api_keys entries)."""
        out: dict[str, str] = {}
        if not self.api_keys:
            return out
        for entry in self.api_keys.split(","):
            entry = entry.strip()
            parts = entry.split(":")
            if len(parts) < 2:
                continue
            key = parts[1].strip()
            tier = parts[2].strip() if len(parts) >= 3 and parts[2].strip() else "free"
            out[key] = tier
        return out


__all__ = ["ProxySettings"]
