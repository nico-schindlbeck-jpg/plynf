# SPDX-License-Identifier: Apache-2.0
"""AWS Bedrock ``ConverseStream`` (``POST /model/{id}/converse-stream``).

Unlike every other Plynf front door, Bedrock streaming is *not* HTTP SSE — it
is the AWS event-stream binary protocol (``vnd.amazon.eventstream``): each
event is a length-prefixed message with a CRC32-checksummed prelude, typed
string headers (``:event-type`` / ``:content-type`` / ``:message-type``), a
JSON payload, and a trailing message CRC32. Plynf computes the result unary
(tool-call interception must finish first) and re-emits it as that binary
sequence, so a boto3 client streaming through Plynf needs no code change and
the per-call savings headers ride along.

The decoder below validates *both* checksums on every frame — so these tests
fail loudly if the wire layout drifts from what the AWS SDKs expect.
"""

from __future__ import annotations

import binascii
import json
import struct

import pytest
from fastapi.testclient import TestClient

from plinth_proxy.api import _synthesize_bedrock_converse_stream, create_app
from plinth_proxy.settings import ProxySettings

_BEDROCK_TOOL_BODY = {
    "messages": [{"role": "user", "content": [{"text": "what is order 12345?"}]}],
    "toolConfig": {
        "tools": [
            {
                "toolSpec": {
                    "name": "get_order",
                    "description": "Fetch order by id",
                    "inputSchema": {"json": {"type": "object"}},
                }
            }
        ]
    },
}

_BEDROCK_PLAIN_BODY = {
    "messages": [{"role": "user", "content": [{"text": "hello"}]}],
}

_MODEL_PATH = "/model/anthropic.claude-3-5-sonnet-20241022-v2:0/converse-stream"


@pytest.fixture
def demo_client():
    return TestClient(create_app(ProxySettings(demo_mode=True)))


def _decode_eventstream(raw: bytes) -> list[tuple[dict[str, str], dict]]:
    """Decode an AWS ``vnd.amazon.eventstream`` byte stream into events.

    Returns ``[(headers, payload), ...]`` and asserts the prelude CRC32 and the
    message CRC32 on every frame — exactly the integrity checks a real AWS SDK
    runs, so a framing bug surfaces here as an assertion failure.
    """
    events: list[tuple[dict[str, str], dict]] = []
    offset = 0
    while offset < len(raw):
        total_len, headers_len = struct.unpack(">II", raw[offset : offset + 8])
        prelude_crc = struct.unpack(">I", raw[offset + 8 : offset + 12])[0]
        assert binascii.crc32(raw[offset : offset + 8]) & 0xFFFFFFFF == prelude_crc

        message = raw[offset : offset + total_len]
        message_crc = struct.unpack(">I", message[-4:])[0]
        assert binascii.crc32(message[:-4]) & 0xFFFFFFFF == message_crc

        headers: dict[str, str] = {}
        pos = 12
        headers_end = 12 + headers_len
        while pos < headers_end:
            name_len = message[pos]
            pos += 1
            name = message[pos : pos + name_len].decode("utf-8")
            pos += name_len
            value_type = message[pos]
            pos += 1
            assert value_type == 7  # UTF-8 string
            value_len = struct.unpack(">H", message[pos : pos + 2])[0]
            pos += 2
            headers[name] = message[pos : pos + value_len].decode("utf-8")
            pos += value_len

        payload = message[headers_end : total_len - 4]
        events.append((headers, json.loads(payload.decode("utf-8")) if payload else {}))
        offset += total_len
    return events


def _agg_text(events: list[tuple[dict[str, str], dict]]) -> str:
    return "".join(
        p["delta"]["text"]
        for h, p in events
        if h[":event-type"] == "contentBlockDelta" and "text" in p.get("delta", {})
    )


def test_stream_is_binary_eventstream_with_valid_crcs(demo_client):
    r = demo_client.post(_MODEL_PATH, json=_BEDROCK_TOOL_BODY)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/vnd.amazon.eventstream")
    assert r.headers["cache-control"] == "no-cache"

    # _decode_eventstream asserts both CRC32s per frame; a bad layout raises.
    events = _decode_eventstream(r.content)
    assert events
    # Every event carries the standard Bedrock headers.
    for headers, _ in events:
        assert headers[":message-type"] == "event"
        assert headers[":content-type"] == "application/json"


def test_stream_brackets_with_message_start_and_metadata(demo_client):
    r = demo_client.post(_MODEL_PATH, json=_BEDROCK_TOOL_BODY)
    events = _decode_eventstream(r.content)
    types = [h[":event-type"] for h, _ in events]
    assert types[0] == "messageStart"
    assert types[-2] == "messageStop"
    assert types[-1] == "metadata"
    start = events[0][1]
    assert start["role"] == "assistant"
    stop = next(p for h, p in events if h[":event-type"] == "messageStop")
    assert stop["stopReason"]  # non-empty (end_turn / tool_use / …)
    meta = events[-1][1]
    assert "usage" in meta


def test_stream_text_deltas_reconstruct_unary_output(demo_client):
    # The aggregated contentBlockDelta text must equal the unary response's
    # assistant text — the stream is the same shaped result, reframed.
    unary = demo_client.post(
        "/model/anthropic.claude-3-5-sonnet-20241022-v2:0/converse",
        json=_BEDROCK_TOOL_BODY,
    )
    assert unary.status_code == 200
    unary_text = "".join(
        b.get("text", "")
        for b in unary.json()["output"]["message"]["content"]
        if "text" in b
    )

    r = demo_client.post(_MODEL_PATH, json=_BEDROCK_TOOL_BODY)
    events = _decode_eventstream(r.content)
    assert _agg_text(events) == unary_text


def test_stream_carries_savings_and_request_id(demo_client):
    r = demo_client.post(_MODEL_PATH, json=_BEDROCK_TOOL_BODY)
    assert int(r.headers["x-plynf-tool-calls"]) >= 1
    assert int(r.headers["x-plynf-raw-tokens"]) >= int(r.headers["x-plynf-shaped-tokens"])
    assert r.headers["x-request-id"].startswith("req_")


def test_unary_converse_still_returns_json(demo_client):
    r = demo_client.post(
        "/model/anthropic.claude-3-5-sonnet-20241022-v2:0/converse",
        json=_BEDROCK_PLAIN_BODY,
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["output"]["message"]["role"] == "assistant"


async def test_tool_use_block_streams_contentblockstart_and_input():
    # The mock resolves get_order server-side, so the final body never carries
    # an unresolved toolUse block — drive the synthesizer directly to cover the
    # tool-use branch (contentBlockStart + toolUse input delta) deterministically.
    final = {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": "tooluse_1",
                            "name": "get_order",
                            "input": {"order_id": "12345"},
                        }
                    }
                ],
            }
        },
        "stopReason": "tool_use",
        "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
        "metrics": {"latencyMs": 0},
    }
    chunks = [c async for c in _synthesize_bedrock_converse_stream(final)]
    events = _decode_eventstream(b"".join(chunks))
    assert [h[":event-type"] for h, _ in events] == [
        "messageStart",
        "contentBlockStart",
        "contentBlockDelta",
        "contentBlockStop",
        "messageStop",
        "metadata",
    ]
    start = next(p for h, p in events if h[":event-type"] == "contentBlockStart")
    assert start["start"]["toolUse"]["name"] == "get_order"
    assert start["start"]["toolUse"]["toolUseId"] == "tooluse_1"
    # Bedrock streams tool input as a serialized JSON *string*.
    delta = next(p for h, p in events if h[":event-type"] == "contentBlockDelta")
    assert json.loads(delta["delta"]["toolUse"]["input"]) == {"order_id": "12345"}
    stop = next(p for h, p in events if h[":event-type"] == "messageStop")
    assert stop["stopReason"] == "tool_use"
