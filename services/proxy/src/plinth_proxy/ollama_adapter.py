# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Ollama native API (``POST /api/chat``) → OpenAI ``/v1/chat/completions``.

Same two-direction pattern as :mod:`anthropic_adapter`, :mod:`gemini_adapter`,
:mod:`cohere_adapter` and :mod:`bedrock_adapter`. Ollama's native chat API is
close to OpenAI's (a ``messages`` array, ``tools`` as ``{type: "function",
function: {...}}``) but diverges in a few ways:

* Inference knobs live under an ``options`` object: ``num_predict`` →
  ``max_tokens``, plus ``temperature`` / ``top_p`` / ``stop`` / ``seed``.
* Top-level ``format`` selects structured output — ``"json"`` maps to OpenAI's
  ``response_format: {"type": "json_object"}`` and a JSON-Schema object maps to
  ``{"type": "json_schema", ...}``.
* ``tool_calls`` carry ``{function: {name, arguments}}`` with ``arguments`` as a
  JSON *object* (OpenAI uses a JSON *string* and adds an ``id`` / ``type``).
* ``tool`` messages carry ``tool_name`` but no ``tool_call_id``.
* A message's ``images`` (base64) have no text equivalent and are dropped.
* Responses wrap the assistant turn in ``message``, use ``done`` /
  ``done_reason``, and report token counts as ``prompt_eval_count`` (input) and
  ``eval_count`` (output).

Streaming is Ollama's newline-delimited JSON (NDJSON), not SSE; the proxy runs
the request unary (tool-call interception must finish first) and replays the
shaped final message as NDJSON (see ``_synthesize_ollama_ndjson`` in
:mod:`.api`).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

UTC = timezone.utc

# ---------------------------------------------------------------------------
# Request: Ollama → OpenAI
# ---------------------------------------------------------------------------


def ollama_chat_request_to_openai(
    body: dict[str, Any], model: str | None = None
) -> dict[str, Any]:
    """Translate an Ollama ``/api/chat`` request body into OpenAI's shape."""
    messages: list[dict[str, Any]] = []
    for m in body.get("messages") or []:
        if not isinstance(m, dict):
            continue
        messages.append(_translate_message_request(m))

    out: dict[str, Any] = {
        "model": model or body.get("model") or "gpt-4o",
        "messages": messages,
    }

    # tools — Ollama already uses the OpenAI {type, function} envelope.
    fn_tools: list[dict[str, Any]] = []
    for t in body.get("tools") or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function")
        if not fn:
            continue
        fn_tools.append(
            {
                "type": "function",
                "function": {
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters") or {"type": "object"},
                },
            }
        )
    if fn_tools:
        out["tools"] = fn_tools

    _apply_inference_params(out, body)
    return out


def _apply_inference_params(out: dict[str, Any], body: dict[str, Any]) -> None:
    """Map Ollama's ``format`` + ``options`` onto an OpenAI request in place.

    Shared by the ``/api/chat`` and ``/api/generate`` translators: ``format``
    ("json" or a JSON-Schema object) → ``response_format``, and the nested
    ``options`` knobs (``num_predict`` → ``max_tokens``, plus temperature /
    top_p / stop / seed) onto their OpenAI equivalents.
    """
    fmt = body.get("format")
    if fmt == "json":
        out["response_format"] = {"type": "json_object"}
    elif isinstance(fmt, dict):
        out["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "response", "schema": fmt},
        }

    opts = body.get("options")
    if isinstance(opts, dict):
        if "temperature" in opts:
            out["temperature"] = opts["temperature"]
        if "top_p" in opts:
            out["top_p"] = opts["top_p"]
        if "seed" in opts:
            out["seed"] = opts["seed"]
        num_predict = opts.get("num_predict")
        if isinstance(num_predict, int) and num_predict > 0:
            out["max_tokens"] = num_predict
        stop = opts.get("stop")
        if stop:
            out["stop"] = stop


def ollama_generate_request_to_openai(
    body: dict[str, Any], model: str | None = None
) -> dict[str, Any]:
    """Translate an Ollama ``/api/generate`` request into OpenAI chat shape.

    ``/api/generate`` is single-turn: an optional ``system`` string plus a
    ``prompt`` become a ``messages`` array. It predates tool-calling, so no
    tools are translated. ``format`` / ``options`` are applied as for
    ``/api/chat``. Stateful ``context`` tokens have no stateless equivalent and
    are dropped.
    """
    messages: list[dict[str, Any]] = []
    system = body.get("system")
    if isinstance(system, str) and system:
        messages.append({"role": "system", "content": system})
    prompt = body.get("prompt")
    messages.append({"role": "user", "content": prompt if isinstance(prompt, str) else ""})

    out: dict[str, Any] = {
        "model": model or body.get("model") or "gpt-4o",
        "messages": messages,
    }
    _apply_inference_params(out, body)
    return out


def _translate_message_request(m: dict[str, Any]) -> dict[str, Any]:
    """One Ollama message → one OpenAI message."""
    role = m.get("role", "user")
    content = m.get("content")
    text = content if isinstance(content, str) else ("" if content is None else json.dumps(content))

    if role == "tool":
        # Ollama tool results carry tool_name, not tool_call_id — best-effort.
        return {
            "role": "tool",
            "tool_call_id": m.get("tool_call_id") or m.get("tool_name") or "",
            "content": text,
        }

    if role == "assistant":
        msg: dict[str, Any] = {"role": "assistant", "content": text or None}
        tool_calls = m.get("tool_calls")
        if tool_calls:
            msg["tool_calls"] = _tool_calls_in(tool_calls)
        return msg

    # user / system (and any unknown role) → plain text content.
    return {"role": role, "content": text}


def _tool_calls_in(tool_calls: list[Any]) -> list[dict[str, Any]]:
    """Ollama tool_calls → OpenAI (arguments object → JSON string, add id/type)."""
    out: list[dict[str, Any]] = []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        if not isinstance(args, str):
            args = json.dumps(args or {})
        out.append(
            {
                "id": tc.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {"name": fn.get("name", ""), "arguments": args},
            }
        )
    return out


# ---------------------------------------------------------------------------
# Response: OpenAI → Ollama
# ---------------------------------------------------------------------------

# OpenAI finish_reason → Ollama done_reason. Ollama reports "stop" for normal
# completions (including tool calls) and "length" when truncated.
_DONE_REASON_MAP = {
    "stop": "stop",
    "tool_calls": "stop",
    "length": "length",
    "content_filter": "stop",
}


def openai_response_to_ollama_chat(
    resp: dict[str, Any], model: str | None = None
) -> dict[str, Any]:
    """Translate an OpenAI chat-completion result into an Ollama ``/api/chat``
    response. ``model`` echoes the id the caller used (so a provider-prefixed
    id from the catalog comes back unchanged)."""
    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}

    message: dict[str, Any] = {"role": "assistant", "content": msg.get("content") or ""}
    tool_calls = msg.get("tool_calls") or []
    if tool_calls:
        message["tool_calls"] = _tool_calls_out(tool_calls)

    finish = choice.get("finish_reason") or "stop"
    usage = resp.get("usage") or {}

    return {
        "model": model or resp.get("model") or "",
        "created_at": _created_at(resp.get("created")),
        "message": message,
        "done": True,
        "done_reason": _DONE_REASON_MAP.get(finish, "stop"),
        "prompt_eval_count": usage.get("prompt_tokens", 0) or 0,
        "eval_count": usage.get("completion_tokens", 0) or 0,
    }


def _tool_calls_out(tool_calls: list[Any]) -> list[dict[str, Any]]:
    """OpenAI tool_calls → Ollama ({function:{name, arguments-as-object}})."""
    out: list[dict[str, Any]] = []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        raw = fn.get("arguments")
        if isinstance(raw, str):
            try:
                args = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                args = {}
        elif isinstance(raw, dict):
            args = raw
        else:
            args = {}
        out.append({"function": {"name": fn.get("name", ""), "arguments": args}})
    return out


def openai_response_to_ollama_generate(
    resp: dict[str, Any], model: str | None = None
) -> dict[str, Any]:
    """Translate an OpenAI chat result into an Ollama ``/api/generate`` response.

    ``/api/generate`` carries the assistant text as a flat ``response`` string
    (no ``message`` wrapper, no tool calls — it predates tool-calling)."""
    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    finish = choice.get("finish_reason") or "stop"
    usage = resp.get("usage") or {}
    return {
        "model": model or resp.get("model") or "",
        "created_at": _created_at(resp.get("created")),
        "response": msg.get("content") or "",
        "done": True,
        "done_reason": _DONE_REASON_MAP.get(finish, "stop"),
        "prompt_eval_count": usage.get("prompt_tokens", 0) or 0,
        "eval_count": usage.get("completion_tokens", 0) or 0,
    }


def _created_at(created: Any) -> str:
    """RFC3339 timestamp for Ollama's ``created_at`` from OpenAI's unix int."""
    if isinstance(created, (int, float)) and created > 0:
        dt = datetime.fromtimestamp(created, tz=UTC)
    else:
        dt = datetime.now(UTC)
    return dt.isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Model listing: OpenAI ListModels → Ollama /api/tags
# ---------------------------------------------------------------------------


def ollama_tags_from_models(models: dict[str, Any]) -> dict[str, Any]:
    """Reshape an OpenAI ``ListModels`` dict into Ollama's ``/api/tags`` body.

    Each OpenAI model id becomes an Ollama tag whose ``name`` / ``model`` is that
    id verbatim — so a provider-prefixed id from the aggregated catalog
    (``groq/llama-3.3-70b``) lists here and is directly usable as ``/api/chat``'s
    ``model``. Duration / digest / size fields Ollama clients tolerate as blanks.
    """
    out: list[dict[str, Any]] = []
    for m in (models or {}).get("data") or []:
        if not isinstance(m, dict):
            continue
        mid = m.get("id")
        if not isinstance(mid, str) or not mid:
            continue
        out.append(
            {
                "name": mid,
                "model": mid,
                "modified_at": "",
                "size": 0,
                "digest": "",
                "details": {
                    "family": m.get("owned_by") or "",
                    "format": "api",
                    "parameter_size": "",
                    "quantization_level": "",
                },
            }
        )
    return {"models": out}


__all__ = [
    "ollama_chat_request_to_openai",
    "ollama_generate_request_to_openai",
    "openai_response_to_ollama_chat",
    "openai_response_to_ollama_generate",
    "ollama_tags_from_models",
]
