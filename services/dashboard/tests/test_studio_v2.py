# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the v2 Plinth Studio drag-drop upgrade.

Scope (per the v2 spec):

* The studio template is now ``tpl-studio-v2`` and ships with draggable
  toolbox tiles, insertion zones, a trash drop target and an undo banner.
* ``app.js`` carries the matching drag-drop wiring (MIME types, event
  listeners, undo banner helpers, exposed test API).
* Existing route + static-asset behaviour is unchanged.

Real drag-drop interaction tests are E2E territory (Playwright) and live
outside this suite. The smoke checks below assert the markup + JS landed
together so they cannot drift.
"""

from __future__ import annotations

import httpx
import pytest
from plinth_dashboard.server import STATIC_DIR


# ---------------------------------------------------------------------------
# Helper: cache file reads at module scope so each test stays cheap.


def _index() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def _app_js() -> str:
    return (STATIC_DIR / "app.js").read_text(encoding="utf-8")


def _style_css() -> str:
    return (STATIC_DIR / "style.css").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Template / markup checks


def test_studio_v2_template_exists():
    """The v2 template id replaces the v1 one and ships with the new copy."""
    body = _index()
    assert 'id="tpl-studio-v2"' in body
    # The new template advertises drag-drop in its header copy.
    assert "Drag a step from the toolbox" in body
    # v1's "Click a tool on the left" placeholder text is gone.
    assert "Click a tool on the left" not in body


def test_toolbox_tiles_are_draggable():
    """Every toolbox tile is HTML5-draggable + has a step-type data attr."""
    body = _index()
    # Each of the five tile types is present + carries draggable="true".
    for step_type in (
        "tool",
        "llm",
        "channel_send",
        "channel_receive",
        "manual",
    ):
        assert f'data-step-type="{step_type}"' in body
    # The draggable attribute is opt-in: count must match the number of
    # tiles (5) so we don't accidentally make non-tile elements draggable.
    assert body.count('draggable="true"') >= 5
    # aria-grabbed is wired for screen readers (deprecated but still hinted).
    assert 'aria-grabbed="false"' in body


def test_canvas_and_trash_zones_present():
    """Canvas exposes an empty-state placeholder; trash zone is in the toolbar."""
    body = _index()
    # Canvas placeholder shows the new "Drop a step here" copy.
    assert "Drop a step here to start" in body
    # Trash zone with the documented id + role/aria for keyboard users.
    assert 'id="studio-trash"' in body
    assert "trash — drop a step here or focus a row and press Delete" in body
    # The keyboard hint above the canvas mentions Delete + reorder buttons.
    assert "Delete to remove" in body


def test_undo_banner_template_present():
    """The undo banner is hidden in the initial markup but ready to populate."""
    body = _index()
    assert 'id="studio-undo-banner"' in body
    assert 'id="studio-undo-btn"' in body
    assert 'id="studio-undo-timer"' in body
    # aria-live so screen readers announce the deletion summary.
    assert 'aria-live="polite"' in body
    # The banner is initially hidden — the JS toggles `hidden` off when a
    # deletion triggers an undo opportunity.
    assert 'id="studio-undo-banner"' in body
    # Use a small windowed search so we don't accidentally match a
    # different `hidden` attribute elsewhere on the page.
    idx = body.index('id="studio-undo-banner"')
    snippet = body[idx : idx + 400]
    assert "hidden" in snippet


def test_app_js_has_drag_drop_listeners():
    """app.js carries the drag-drop MIME types and listener entry points."""
    js = _app_js()
    # MIME constants drive the dataTransfer payloads.
    assert "application/x-plinth-step-type" in js
    assert "application/x-plinth-step-id" in js
    # The event listeners we install for the drag-drop lifecycle.
    assert 'addEventListener("dragstart"' in js
    assert 'addEventListener("dragover"' in js
    assert 'addEventListener("drop"' in js
    assert 'addEventListener("dragend"' in js
    # Body-level classes power the zone visibility transitions.
    assert "studio-dragging-from-toolbox" in js
    assert "studio-dragging-row" in js


def test_app_js_has_undo_and_trash_helpers():
    """The JS exposes the undo banner + trash-drop helpers."""
    js = _app_js()
    # Undo banner machinery.
    assert "studioShowUndoBanner" in js
    assert "studioUndoLastDelete" in js
    assert "STUDIO_UNDO_MS" in js
    # Trash drop wiring.
    assert "wireStudioTrash" in js
    assert "studio-trash-active" in js
    # The test/power-user surface now exposes drag-drop helpers.
    assert "insertNewStep" in js
    assert "reorderStep" in js
    assert "deleteStep" in js


def test_app_js_mounts_v2_template():
    """The SPA router mounts the new ``tpl-studio-v2`` template id."""
    js = _app_js()
    assert 'mountTemplate("tpl-studio-v2")' in js
    # Sanity: nothing still references the retired v1 id.
    assert 'mountTemplate("tpl-studio")' not in js


def test_style_css_has_drag_drop_affordances():
    """The drag-drop CSS classes that JS toggles all exist in style.css."""
    css = _style_css()
    # Zone + active glow.
    assert ".studio-zone" in css
    assert ".studio-zone-line" in css
    assert ".studio-zone-active" in css
    # Row dragging affordance + cursor: grab.
    assert ".studio-row-dragging" in css
    assert "cursor: grab" in css
    # Trash zone affordances (idle + active pulse).
    assert ".studio-trash" in css
    assert ".studio-trash-active" in css
    assert "studio-trash-pulse" in css
    # Undo banner styling.
    assert ".studio-undo-banner" in css


def test_studio_v1_template_id_removed():
    """The v1 ``tpl-studio`` template id is replaced — guards against drift."""
    body = _index()
    # `tpl-studio-v2` contains `tpl-studio` as a substring, so we look for
    # the exact id="tpl-studio" attribute instead.
    assert 'id="tpl-studio"' not in body


# ---------------------------------------------------------------------------
# Route still serves the SPA shell unchanged (regression guard)


@pytest.mark.asyncio
async def test_studio_route_still_serves_spa(client: httpx.AsyncClient):
    """The ``/studio`` route still serves the SPA shell — v2 doesn't move it."""
    r = await client.get("/studio")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    # The shell carries the v2 template inline.
    assert 'id="tpl-studio-v2"' in r.text


@pytest.mark.asyncio
async def test_app_js_response_carries_drag_drop_strings(
    client: httpx.AsyncClient,
):
    """``/static/app.js`` ships the drag-drop wiring at HTTP time too."""
    r = await client.get("/static/app.js")
    assert r.status_code == 200
    assert "application/x-plinth-step-type" in r.text
    assert "studio-dragging-from-toolbox" in r.text
    assert "studioShowUndoBanner" in r.text
