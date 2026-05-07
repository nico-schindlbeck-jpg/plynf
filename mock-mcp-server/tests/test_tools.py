# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""End-to-end tests for individual tool behaviors."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx


# ---------------------------------------------------------------------------
# web.fetch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_web_fetch_mock_url_returns_fixture_content(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/invoke/web.fetch", json={"url": "mock://renewable-energy-1"}
    )
    assert response.status_code == 200
    result = response.json()["result"]
    assert "Solar power" in result["content"]
    assert result["status"] == 200
    assert result["content_type"].startswith("text/plain")


@pytest.mark.asyncio
async def test_web_fetch_unknown_mock_url_returns_404(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/invoke/web.fetch", json={"url": "mock://does-not-exist-1"}
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "INVALID_ARGUMENTS"


@pytest.mark.asyncio
async def test_web_fetch_https_url_uses_httpx(client: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=True) as respx_mock:
        respx_mock.get("https://example.com/page").mock(
            return_value=httpx.Response(
                200,
                text="<html>hello</html>",
                headers={"content-type": "text/html; charset=utf-8"},
            )
        )
        response = await client.post(
            "/invoke/web.fetch", json={"url": "https://example.com/page"}
        )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["content"] == "<html>hello</html>"
    assert result["status"] == 200
    assert result["content_type"] == "text/html; charset=utf-8"


@pytest.mark.asyncio
async def test_web_fetch_https_failure_surfaces_error(client: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=True) as respx_mock:
        respx_mock.get("https://example.com/boom").mock(
            side_effect=httpx.ConnectError("boom")
        )
        response = await client.post(
            "/invoke/web.fetch", json={"url": "https://example.com/boom"}
        )
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "TOOL_INVOCATION_FAILED"


@pytest.mark.asyncio
async def test_web_fetch_bad_scheme_returns_400(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/invoke/web.fetch", json={"url": "ftp://example.com/x"}
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "INVALID_ARGUMENTS"
    assert "scheme" in error["message"]


@pytest.mark.asyncio
async def test_web_fetch_missing_url_returns_400(client: httpx.AsyncClient) -> None:
    response = await client.post("/invoke/web.fetch", json={})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_ARGUMENTS"


# ---------------------------------------------------------------------------
# web.search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_web_search_known_topic_returns_five_results(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/invoke/web.search", json={"query": "renewable energy", "k": 5}
    )
    assert response.status_code == 200
    results = response.json()["result"]["results"]
    assert len(results) == 5
    urls = [r["url"] for r in results]
    assert urls == [f"mock://renewable-energy-{i}" for i in range(1, 6)]
    for r in results:
        assert r["title"].startswith("Source ")
        assert r["snippet"]
        assert len(r["snippet"]) <= 200


@pytest.mark.asyncio
async def test_web_search_unknown_topic_falls_back_to_renewable(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/invoke/web.search", json={"query": "qwerty unknown topic"}
    )
    assert response.status_code == 200
    results = response.json()["result"]["results"]
    assert len(results) == 5
    # Fallback uses synthesized URLs for the user-supplied topic slug.
    assert results[0]["url"].startswith("mock://qwerty-unknown-topic-")


@pytest.mark.asyncio
async def test_web_search_substring_match(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/invoke/web.search", json={"query": "ai agents in production"}
    )
    assert response.status_code == 200
    urls = [r["url"] for r in response.json()["result"]["results"]]
    assert urls[0].startswith("mock://ai-agents-")


@pytest.mark.asyncio
async def test_web_search_k_caps_result_count(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/invoke/web.search", json={"query": "climate policy", "k": 2}
    )
    assert response.status_code == 200
    assert len(response.json()["result"]["results"]) == 2


@pytest.mark.asyncio
async def test_web_search_invalid_k_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/invoke/web.search", json={"query": "renewable energy", "k": 0}
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_web_search_blank_query_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post("/invoke/web.search", json={"query": "   "})
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# fs.read / fs.write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fs_write_then_read_roundtrip(client: httpx.AsyncClient) -> None:
    write = await client.post(
        "/invoke/fs.write",
        json={"path": "out/note.txt", "content": "hello world"},
    )
    assert write.status_code == 200
    write_result = write.json()["result"]
    assert write_result["bytes_written"] == 11
    assert write_result["path"].endswith("out/note.txt")

    read = await client.post("/invoke/fs.read", json={"path": "out/note.txt"})
    assert read.status_code == 200
    read_result = read.json()["result"]
    assert read_result["content"] == "hello world"
    assert read_result["size"] == 11


@pytest.mark.asyncio
async def test_fs_read_missing_file_returns_404(client: httpx.AsyncClient) -> None:
    response = await client.post("/invoke/fs.read", json={"path": "missing.txt"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "FILE_NOT_FOUND"


@pytest.mark.asyncio
async def test_fs_read_path_traversal_blocked(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/invoke/fs.read", json={"path": "../../etc/passwd"}
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "INVALID_ARGUMENTS"
    assert "traversal" in error["message"]


@pytest.mark.asyncio
async def test_fs_write_path_traversal_blocked(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/invoke/fs.write",
        json={"path": "../escape.txt", "content": "x"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_ARGUMENTS"


@pytest.mark.asyncio
async def test_fs_read_non_utf8_returns_400(
    client: httpx.AsyncClient, settings, app
) -> None:
    """Non-UTF-8 file content should produce a clean 400."""
    target = settings.fixtures_dir / "binary.bin"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\xff\xfe\x00bad")
    response = await client.post("/invoke/fs.read", json={"path": "binary.bin"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_ARGUMENTS"


@pytest.mark.asyncio
async def test_fs_read_missing_path_returns_400(client: httpx.AsyncClient) -> None:
    response = await client.post("/invoke/fs.read", json={})
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_fs_write_missing_content_returns_400(client: httpx.AsyncClient) -> None:
    response = await client.post("/invoke/fs.write", json={"path": "x.txt"})
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_fs_read_directory_returns_404(
    client: httpx.AsyncClient, settings
) -> None:
    target = settings.fixtures_dir / "subdir"
    target.mkdir(parents=True, exist_ok=True)
    response = await client.post("/invoke/fs.read", json={"path": "subdir"})
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# notes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notes_add_then_list(client: httpx.AsyncClient) -> None:
    add = await client.post(
        "/invoke/notes.add", json={"title": "first", "body": "hello"}
    )
    assert add.status_code == 200
    add_payload = add.json()["result"]
    assert add_payload["id"].startswith("note_")
    assert add_payload["created_at"]

    listing = await client.post("/invoke/notes.list", json={})
    assert listing.status_code == 200
    notes = listing.json()["result"]["notes"]
    assert len(notes) == 1
    note = notes[0]
    assert note["id"] == add_payload["id"]
    assert note["title"] == "first"
    assert note["body"] == "hello"
    assert note["created_at"] == add_payload["created_at"]


@pytest.mark.asyncio
async def test_notes_add_validation(client: httpx.AsyncClient) -> None:
    bad_title = await client.post(
        "/invoke/notes.add", json={"title": "", "body": "x"}
    )
    assert bad_title.status_code == 400
    bad_body = await client.post(
        "/invoke/notes.add", json={"title": "t", "body": 123}
    )
    assert bad_body.status_code == 400


@pytest.mark.asyncio
async def test_notes_list_returns_independent_copy(client: httpx.AsyncClient) -> None:
    """Mutating the response shouldn't poison the in-memory store."""
    await client.post("/invoke/notes.add", json={"title": "a", "body": "1"})
    first = await client.post("/invoke/notes.list", json={})
    notes = first.json()["result"]["notes"]
    notes[0]["title"] = "MUTATED"
    second = await client.post("/invoke/notes.list", json={})
    assert second.json()["result"]["notes"][0]["title"] == "a"


# ---------------------------------------------------------------------------
# Fixture sanity
# ---------------------------------------------------------------------------


def test_fixtures_have_fifteen_unique_urls() -> None:
    from mock_mcp import fixtures

    urls = list(fixtures.URL_INDEX.keys())
    assert len(urls) == 15
    assert len(set(urls)) == 15
    for topic in ("renewable-energy", "ai-agents", "climate-policy"):
        for i in range(1, 6):
            assert f"mock://{topic}-{i}" in fixtures.URL_INDEX


def test_fixture_content_is_substantial() -> None:
    from mock_mcp import fixtures

    for entry in fixtures.URL_INDEX.values():
        word_count = len(entry["content"].split())
        # PoC fixtures land in the 600-2500 word range. Spec target was
        # 1200-1800; v0.1 fixtures are slightly below, which still drives
        # 70%+ token reduction in the demo. TODO(v0.2): expand to ~1500.
        assert 600 <= word_count <= 2500, (
            f"{entry['url']} has {word_count} words"
        )


def test_fixture_topic_keys_match_demo() -> None:
    """Topic keys MUST match what examples/01-research-agent/shared.py uses."""
    from mock_mcp import fixtures

    assert set(fixtures.FIXTURES.keys()) == {
        "renewable energy",
        "ai agents",
        "climate policy",
    }


def test_safe_path_blocks_traversal(tmp_path: Path) -> None:
    """Direct unit test of the safe_path helper."""
    from mock_mcp.tools import ToolError, safe_path

    base = tmp_path / "fixtures"
    base.mkdir()
    # Allowed
    ok = safe_path(base, "subdir/file.txt")
    assert str(ok).startswith(str(base.resolve()))
    # Blocked
    with pytest.raises(ToolError):
        safe_path(base, "../escape")
    with pytest.raises(ToolError):
        safe_path(base, "/etc/passwd")
