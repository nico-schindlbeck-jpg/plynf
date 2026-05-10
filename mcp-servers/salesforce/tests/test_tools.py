# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the salesforce-mcp server endpoints + tools."""

from __future__ import annotations

import json as _json

import pytest
import respx
from httpx import Response

from salesforce_mcp.tools import parse_record_id, parse_sobject_type


# Test instance URL on a permitted ``.salesforce.test`` host (registered in
# the default ``allowed_host_suffixes`` set).
INSTANCE_URL = "https://acme.my.salesforce.test"
API_VERSION = "v60.0"
RECORD_ID = "0017F00000abcdeFG1"  # 18-char alphanumeric


def _auth_headers(instance_url: str | None = INSTANCE_URL) -> dict[str, str]:
    headers = {"Authorization": "Bearer t"}
    if instance_url is not None:
        headers["X-Plinth-OAuth-InstanceUrl"] = instance_url
    return headers


# ---------------------------------------------------------------------------
# parse_sobject_type — input validation
# ---------------------------------------------------------------------------


def test_parse_sobject_type_valid() -> None:
    assert parse_sobject_type("Lead") == "Lead"
    assert parse_sobject_type("Custom_Object__c") == "Custom_Object__c"


def test_parse_sobject_type_rejects_traversal() -> None:
    from salesforce_mcp.tools import ToolError

    with pytest.raises(ToolError):
        parse_sobject_type("../etc")
    with pytest.raises(ToolError):
        parse_sobject_type("Lead/Contact")


def test_parse_sobject_type_rejects_empty() -> None:
    from salesforce_mcp.tools import ToolError

    with pytest.raises(ToolError):
        parse_sobject_type("")
    with pytest.raises(ToolError):
        parse_sobject_type(None)


def test_parse_sobject_type_rejects_special_chars() -> None:
    from salesforce_mcp.tools import ToolError

    with pytest.raises(ToolError):
        parse_sobject_type("Lead!")
    with pytest.raises(ToolError):
        parse_sobject_type("Lead Lead")


# ---------------------------------------------------------------------------
# parse_record_id
# ---------------------------------------------------------------------------


def test_parse_record_id_15_char() -> None:
    assert parse_record_id("0017F00000abcdE") == "0017F00000abcdE"


def test_parse_record_id_18_char() -> None:
    assert parse_record_id(RECORD_ID) == RECORD_ID


def test_parse_record_id_rejects_short() -> None:
    from salesforce_mcp.tools import ToolError

    with pytest.raises(ToolError):
        parse_record_id("short")


def test_parse_record_id_rejects_traversal() -> None:
    from salesforce_mcp.tools import ToolError

    with pytest.raises(ToolError):
        parse_record_id("../etc/passwd")


# ---------------------------------------------------------------------------
# /healthz + /tools
# ---------------------------------------------------------------------------


async def test_healthz(client) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "salesforce-mcp"
    assert body["version"] == "1.5.0"


async def test_tools_listing_has_six_tools(client) -> None:
    resp = await client.get("/tools")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["tools"]) == 6
    tool_ids = {t["tool_id"] for t in body["tools"]}
    assert tool_ids == {
        "salesforce.soql_query",
        "salesforce.get_record",
        "salesforce.create_record",
        "salesforce.update_record",
        "salesforce.delete_record",
        "salesforce.list_objects",
    }
    for t in body["tools"]:
        assert t["auth_method"] == "oauth2"
        assert t["auth_config"] == {"provider": "salesforce"}


# ---------------------------------------------------------------------------
# Auth — missing token / instance_url
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_id, payload",
    [
        ("salesforce.soql_query", {"soql": "SELECT Id FROM Lead"}),
        ("salesforce.get_record", {"object_type": "Lead", "record_id": RECORD_ID}),
        (
            "salesforce.create_record",
            {"object_type": "Lead", "fields": {"LastName": "x"}},
        ),
        (
            "salesforce.update_record",
            {"object_type": "Lead", "record_id": RECORD_ID, "fields": {"LastName": "y"}},
        ),
        ("salesforce.delete_record", {"object_type": "Lead", "record_id": RECORD_ID}),
        ("salesforce.list_objects", {}),
    ],
)
async def test_unauthorized_without_bearer(client, tool_id: str, payload: dict) -> None:
    resp = await client.post(
        f"/invoke/{tool_id}",
        json=payload,
        headers={"X-Plinth-OAuth-InstanceUrl": INSTANCE_URL},
    )
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "UNAUTHORIZED"


async def test_soql_query_requires_instance_url(client) -> None:
    resp = await client.post(
        "/invoke/salesforce.soql_query",
        json={"soql": "SELECT Id FROM Lead"},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "SALESFORCE_INSTANCE_URL_MISSING"


async def test_instance_url_must_be_https(client) -> None:
    resp = await client.post(
        "/invoke/salesforce.soql_query",
        json={"soql": "SELECT Id FROM Lead"},
        headers={
            "Authorization": "Bearer t",
            "X-Plinth-OAuth-InstanceUrl": "http://acme.my.salesforce.com",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "SALESFORCE_INSTANCE_URL_INVALID"


async def test_instance_url_host_must_be_known(client) -> None:
    resp = await client.post(
        "/invoke/salesforce.soql_query",
        json={"soql": "SELECT Id FROM Lead"},
        headers={
            "Authorization": "Bearer t",
            "X-Plinth-OAuth-InstanceUrl": "https://attacker.example.com",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "SALESFORCE_INSTANCE_URL_INVALID"


# ---------------------------------------------------------------------------
# salesforce.soql_query
# ---------------------------------------------------------------------------


async def test_soql_query_returns_records(client) -> None:
    captured: dict = {}

    def _capture(request):
        captured["params"] = dict(request.url.params)
        captured["auth"] = request.headers.get("Authorization")
        return Response(
            200,
            json={
                "totalSize": 2,
                "done": True,
                "records": [
                    {"Id": RECORD_ID, "Name": "Acme"},
                    {"Id": "0017F00000abcdeFG2", "Name": "Beta Co."},
                ],
            },
        )

    with respx.mock(assert_all_called=True) as mock:
        mock.get(
            f"{INSTANCE_URL}/services/data/{API_VERSION}/query"
        ).mock(side_effect=_capture)
        resp = await client.post(
            "/invoke/salesforce.soql_query",
            json={"soql": "SELECT Id, Name FROM Account"},
            headers=_auth_headers(),
        )
    assert resp.status_code == 200
    assert captured["auth"] == "Bearer t"
    body = resp.json()["result"]
    assert body["total_size"] == 2
    assert body["done"] is True
    assert len(body["records"]) == 2
    assert captured["params"]["q"] == "SELECT Id, Name FROM Account"


async def test_soql_query_requires_query(client) -> None:
    resp = await client.post(
        "/invoke/salesforce.soql_query",
        json={"soql": ""},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400


async def test_soql_query_propagates_401(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(
            f"{INSTANCE_URL}/services/data/{API_VERSION}/query"
        ).mock(return_value=Response(401, json=[{"errorCode": "INVALID_SESSION_ID"}]))
        resp = await client.post(
            "/invoke/salesforce.soql_query",
            json={"soql": "SELECT Id FROM Lead"},
            headers=_auth_headers(),
        )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


# ---------------------------------------------------------------------------
# salesforce.get_record
# ---------------------------------------------------------------------------


async def test_get_record_returns_record(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(
            f"{INSTANCE_URL}/services/data/{API_VERSION}/sobjects/Lead/{RECORD_ID}"
        ).mock(
            return_value=Response(
                200,
                json={"Id": RECORD_ID, "FirstName": "A", "LastName": "Lead"},
            )
        )
        resp = await client.post(
            "/invoke/salesforce.get_record",
            json={"object_type": "Lead", "record_id": RECORD_ID},
            headers=_auth_headers(),
        )
    assert resp.status_code == 200
    body = resp.json()["result"]
    assert body["Id"] == RECORD_ID
    assert body["LastName"] == "Lead"


async def test_get_record_with_fields_filter(client) -> None:
    captured: dict = {}

    def _capture(request):
        captured["params"] = dict(request.url.params)
        return Response(200, json={"Id": RECORD_ID, "FirstName": "A", "LastName": "Lead"})

    with respx.mock(assert_all_called=True) as mock:
        mock.get(
            f"{INSTANCE_URL}/services/data/{API_VERSION}/sobjects/Lead/{RECORD_ID}"
        ).mock(side_effect=_capture)
        resp = await client.post(
            "/invoke/salesforce.get_record",
            json={
                "object_type": "Lead",
                "record_id": RECORD_ID,
                "fields": ["FirstName", "LastName"],
            },
            headers=_auth_headers(),
        )
    assert resp.status_code == 200
    assert captured["params"]["fields"] == "FirstName,LastName"


async def test_get_record_404(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(
            f"{INSTANCE_URL}/services/data/{API_VERSION}/sobjects/Lead/{RECORD_ID}"
        ).mock(return_value=Response(404, json=[{"errorCode": "NOT_FOUND"}]))
        resp = await client.post(
            "/invoke/salesforce.get_record",
            json={"object_type": "Lead", "record_id": RECORD_ID},
            headers=_auth_headers(),
        )
    assert resp.status_code == 404


async def test_get_record_validates_object_type(client) -> None:
    resp = await client.post(
        "/invoke/salesforce.get_record",
        json={"object_type": "../etc", "record_id": RECORD_ID},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400


async def test_get_record_validates_record_id(client) -> None:
    resp = await client.post(
        "/invoke/salesforce.get_record",
        json={"object_type": "Lead", "record_id": "short"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# salesforce.create_record
# ---------------------------------------------------------------------------


async def test_create_record_returns_id(client) -> None:
    captured: dict = {}

    def _capture(request):
        captured["body"] = request.read()
        return Response(
            201,
            json={"id": RECORD_ID, "success": True, "errors": []},
        )

    with respx.mock(assert_all_called=True) as mock:
        mock.post(
            f"{INSTANCE_URL}/services/data/{API_VERSION}/sobjects/Lead"
        ).mock(side_effect=_capture)
        resp = await client.post(
            "/invoke/salesforce.create_record",
            json={
                "object_type": "Lead",
                "fields": {"FirstName": "A", "LastName": "Test", "Company": "Acme"},
            },
            headers=_auth_headers(),
        )
    assert resp.status_code == 200
    body = resp.json()["result"]
    assert body["id"] == RECORD_ID
    assert body["success"] is True
    assert body["object_type"] == "Lead"
    sent = _json.loads(captured["body"])
    assert sent["FirstName"] == "A"
    assert sent["Company"] == "Acme"


async def test_create_record_requires_fields(client) -> None:
    resp = await client.post(
        "/invoke/salesforce.create_record",
        json={"object_type": "Lead", "fields": {}},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400


async def test_create_record_returns_validation_errors(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(
            f"{INSTANCE_URL}/services/data/{API_VERSION}/sobjects/Lead"
        ).mock(
            return_value=Response(
                400,
                json=[{"errorCode": "REQUIRED_FIELD_MISSING", "message": "Required"}],
            )
        )
        resp = await client.post(
            "/invoke/salesforce.create_record",
            json={"object_type": "Lead", "fields": {"FirstName": "x"}},
            headers=_auth_headers(),
        )
    assert resp.status_code == 502
    body = resp.json()["error"]
    assert body["code"] == "TOOL_INVOCATION_FAILED"
    assert "errors" in body["details"]


# ---------------------------------------------------------------------------
# salesforce.update_record
# ---------------------------------------------------------------------------


async def test_update_record(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.patch(
            f"{INSTANCE_URL}/services/data/{API_VERSION}/sobjects/Lead/{RECORD_ID}"
        ).mock(return_value=Response(204))
        resp = await client.post(
            "/invoke/salesforce.update_record",
            json={
                "object_type": "Lead",
                "record_id": RECORD_ID,
                "fields": {"Status": "Working"},
            },
            headers=_auth_headers(),
        )
    assert resp.status_code == 200
    body = resp.json()["result"]
    assert body["id"] == RECORD_ID
    assert body["updated"] is True


async def test_update_record_requires_fields(client) -> None:
    resp = await client.post(
        "/invoke/salesforce.update_record",
        json={"object_type": "Lead", "record_id": RECORD_ID, "fields": {}},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# salesforce.delete_record
# ---------------------------------------------------------------------------


async def test_delete_record(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.delete(
            f"{INSTANCE_URL}/services/data/{API_VERSION}/sobjects/Lead/{RECORD_ID}"
        ).mock(return_value=Response(204))
        resp = await client.post(
            "/invoke/salesforce.delete_record",
            json={"object_type": "Lead", "record_id": RECORD_ID},
            headers=_auth_headers(),
        )
    assert resp.status_code == 200
    body = resp.json()["result"]
    assert body["deleted"] is True
    assert body["id"] == RECORD_ID


# ---------------------------------------------------------------------------
# salesforce.list_objects
# ---------------------------------------------------------------------------


async def test_list_objects(client) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{INSTANCE_URL}/services/data/{API_VERSION}/sobjects").mock(
            return_value=Response(
                200,
                json={
                    "sobjects": [
                        {
                            "name": "Lead",
                            "label": "Lead",
                            "labelPlural": "Leads",
                            "custom": False,
                            "createable": True,
                            "updateable": True,
                            "deletable": True,
                            "queryable": True,
                        },
                        {
                            "name": "Custom__c",
                            "label": "Custom",
                            "labelPlural": "Customs",
                            "custom": True,
                            "createable": True,
                            "updateable": True,
                            "deletable": False,
                            "queryable": True,
                        },
                    ]
                },
            )
        )
        resp = await client.post(
            "/invoke/salesforce.list_objects",
            json={},
            headers=_auth_headers(),
        )
    assert resp.status_code == 200
    body = resp.json()["result"]
    assert body["count"] == 2
    names = {o["name"] for o in body["objects"]}
    assert names == {"Lead", "Custom__c"}


# ---------------------------------------------------------------------------
# Unknown tool / malformed body
# ---------------------------------------------------------------------------


async def test_unknown_tool_returns_404(client) -> None:
    resp = await client.post(
        "/invoke/salesforce.does_not_exist",
        json={},
        headers=_auth_headers(),
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "TOOL_NOT_FOUND"


async def test_invalid_json_body_returns_400(client) -> None:
    resp = await client.post(
        "/invoke/salesforce.soql_query",
        content=b"not json",
        headers={
            **_auth_headers(),
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 400


async def test_array_body_rejected(client) -> None:
    resp = await client.post(
        "/invoke/salesforce.soql_query",
        json=["not", "an", "object"],
        headers=_auth_headers(),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Instance URL plumbing — request URL contains the host we passed.
# ---------------------------------------------------------------------------


async def test_query_uses_instance_url_in_path(client) -> None:
    """Confirm the request goes to the instance_url we passed via header."""
    captured: dict = {}

    other = "https://other-org.my.salesforce.test"

    def _capture(request):
        captured["host"] = request.url.host
        return Response(200, json={"records": [], "totalSize": 0, "done": True})

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{other}/services/data/{API_VERSION}/query").mock(side_effect=_capture)
        resp = await client.post(
            "/invoke/salesforce.soql_query",
            json={"soql": "SELECT Id FROM Lead"},
            headers=_auth_headers(instance_url=other),
        )
    assert resp.status_code == 200
    assert captured["host"] == "other-org.my.salesforce.test"
