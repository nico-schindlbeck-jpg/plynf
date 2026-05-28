# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the Plynf Policy Engine v1."""

from __future__ import annotations

from pathlib import Path

import pytest

from plinth_proxy.policy_engine import (
    ConnectorPolicy,
    PolicyError,
    RedactRule,
    ToolPolicy,
    apply,
    load_policy,
)

POLICIES_DIR = Path(__file__).resolve().parent.parent / "src" / "plinth_proxy" / "policies"


# ---------------------------------------------------------------------------
# allow_fields
# ---------------------------------------------------------------------------


def test_allow_fields_keeps_only_listed_top_level_keys():
    policy = ToolPolicy(tool="get_lead", allow_fields=("Id", "Name", "Status"))
    raw = {
        "Id": "00Q...",
        "Name": "Jane Doe",
        "Status": "Working",
        "AnnualRevenue": 99_000_000,
        "Description": "lots of text",
        "OwnerId": "005...",
    }
    shaped = apply(raw, policy)
    assert shaped == {"Id": "00Q...", "Name": "Jane Doe", "Status": "Working"}


def test_allow_fields_on_list_projects_each_element():
    policy = ToolPolicy(tool="list_leads", allow_fields=("Id", "Status"))
    raw = [
        {"Id": "1", "Status": "Open", "Notes": "x"},
        {"Id": "2", "Status": "Closed", "Notes": "y"},
    ]
    shaped = apply(raw, policy)
    assert shaped == [{"Id": "1", "Status": "Open"}, {"Id": "2", "Status": "Closed"}]


def test_allow_fields_with_dotted_nested_path():
    policy = ToolPolicy(tool="get_account", allow_fields=("Id", "Owner.Name"))
    raw = {
        "Id": "001",
        "Name": "Acme",
        "Owner": {"Id": "u1", "Name": "Bob", "Email": "bob@x.com"},
        "Description": "secret",
    }
    shaped = apply(raw, policy)
    assert shaped == {"Id": "001", "Owner": {"Name": "Bob"}}


def test_allow_fields_none_returns_input_unchanged():
    policy = ToolPolicy(tool="get_lead", allow_fields=None)
    raw = {"Id": "1", "Anything": "kept"}
    assert apply(raw, policy) == raw


# ---------------------------------------------------------------------------
# deny_fields
# ---------------------------------------------------------------------------


def test_deny_fields_strips_top_level_keys():
    policy = ToolPolicy(tool="get_lead", deny_fields=("Description", "AnnualRevenue"))
    raw = {"Id": "1", "Name": "Jane", "Description": "x", "AnnualRevenue": 99}
    shaped = apply(raw, policy)
    assert shaped == {"Id": "1", "Name": "Jane"}


def test_deny_fields_works_with_nested_dotted_paths():
    policy = ToolPolicy(tool="get_account", deny_fields=("Owner.Email",))
    raw = {
        "Id": "1",
        "Owner": {"Id": "u1", "Name": "Bob", "Email": "bob@x.com"},
    }
    shaped = apply(raw, policy)
    assert shaped == {"Id": "1", "Owner": {"Id": "u1", "Name": "Bob"}}


def test_allow_then_deny_compose():
    policy = ToolPolicy(
        tool="get_account",
        allow_fields=("Id", "Name", "Owner"),
        deny_fields=("Owner.Email",),
    )
    raw = {
        "Id": "1",
        "Name": "Acme",
        "Owner": {"Id": "u1", "Name": "Bob", "Email": "bob@x.com"},
        "Hidden": "should_drop",
    }
    shaped = apply(raw, policy)
    assert "Hidden" not in shaped
    assert "Email" not in shaped["Owner"]
    assert shaped["Owner"]["Name"] == "Bob"


# ---------------------------------------------------------------------------
# strip_metadata
# ---------------------------------------------------------------------------


def test_strip_metadata_removes_common_audit_fields():
    policy = ToolPolicy(tool="get_lead", strip_metadata=True)
    raw = {
        "Id": "1",
        "Name": "Jane",
        "CreatedDate": "2025-01-01",
        "LastModifiedDate": "2025-09-01",
        "SystemModstamp": "2025-09-01",
        "attributes": {"type": "Lead", "url": "/services/data/..."},
    }
    shaped = apply(raw, policy)
    assert shaped == {"Id": "1", "Name": "Jane"}


def test_strip_metadata_recurses_into_nested_dicts():
    policy = ToolPolicy(tool="get_account", strip_metadata=True)
    raw = {
        "Id": "1",
        "Owner": {
            "Id": "u1",
            "Name": "Bob",
            "CreatedDate": "2025-01-01",
            "attributes": {"type": "User"},
        },
    }
    shaped = apply(raw, policy)
    assert shaped == {"Id": "1", "Owner": {"Id": "u1", "Name": "Bob"}}


# ---------------------------------------------------------------------------
# redact_pii
# ---------------------------------------------------------------------------


def test_redact_pii_hash_mode_replaces_value_with_sha256_prefix():
    policy = ToolPolicy(
        tool="get_lead",
        redact_pii=RedactRule(fields=("Email",), mode="hash"),
    )
    raw = {"Id": "1", "Email": "jane@example.com"}
    shaped = apply(raw, policy)
    assert shaped["Email"].startswith("sha256:")
    assert len(shaped["Email"]) == len("sha256:") + 8


def test_redact_pii_mask_mode_replaces_with_stars():
    policy = ToolPolicy(
        tool="get_lead",
        redact_pii=RedactRule(fields=("Email", "Phone"), mode="mask"),
    )
    raw = {"Email": "x@y.com", "Phone": "+49..."}
    shaped = apply(raw, policy)
    assert shaped == {"Email": "***", "Phone": "***"}


def test_redact_pii_remove_mode_drops_field():
    policy = ToolPolicy(
        tool="get_lead",
        redact_pii=RedactRule(fields=("Email",), mode="remove"),
    )
    raw = {"Id": "1", "Email": "x@y.com"}
    shaped = apply(raw, policy)
    assert shaped == {"Id": "1"}


def test_redact_pii_handles_nested_dotted_paths():
    policy = ToolPolicy(
        tool="get_account",
        redact_pii=RedactRule(fields=("Owner.Email",), mode="mask"),
    )
    raw = {"Id": "1", "Owner": {"Name": "Bob", "Email": "bob@x.com"}}
    shaped = apply(raw, policy)
    assert shaped["Owner"]["Name"] == "Bob"
    assert shaped["Owner"]["Email"] == "***"


# ---------------------------------------------------------------------------
# max_response_tokens
# ---------------------------------------------------------------------------


def test_max_response_tokens_truncates_long_list():
    policy = ToolPolicy(tool="list_leads", max_response_tokens=10)
    raw = [{"Id": f"id-{i}", "Name": f"User {i}", "Padding": "x" * 200} for i in range(50)]
    shaped = apply(raw, policy)
    assert isinstance(shaped, list)
    assert len(shaped) < 50
    # The truncation marker should be present.
    assert any(isinstance(item, dict) and item.get("_plynf_truncated") for item in shaped)


def test_max_response_tokens_truncates_long_string():
    policy = ToolPolicy(tool="get_doc", max_response_tokens=20)
    raw = "abcdefghijklmnop" * 200  # 3200 chars ≈ 800 tokens
    shaped = apply(raw, policy)
    assert isinstance(shaped, str)
    assert shaped.endswith("…[truncated]")
    assert len(shaped) < 200


def test_max_response_tokens_pass_through_when_under_budget():
    policy = ToolPolicy(tool="get_lead", max_response_tokens=10_000)
    raw = {"Id": "1", "Name": "Jane"}
    assert apply(raw, policy) == raw


# ---------------------------------------------------------------------------
# block_write_actions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool",
    ["create_lead", "update_account", "delete_contact", "send_email"],
)
def test_block_write_actions_raises_for_known_prefixes(tool):
    policy = ToolPolicy(tool=tool, block_write_actions=True)
    with pytest.raises(PolicyError):
        apply({"any": "data"}, policy)


def test_block_write_actions_allows_reads():
    policy = ToolPolicy(tool="get_lead", block_write_actions=True)
    raw = {"Id": "1"}
    assert apply(raw, policy) == raw


# ---------------------------------------------------------------------------
# YAML loader
# ---------------------------------------------------------------------------


def test_load_policy_salesforce_default():
    policy = load_policy(POLICIES_DIR / "salesforce.default.yaml")
    assert isinstance(policy, ConnectorPolicy)
    assert policy.connector == "salesforce"
    assert "get_lead" in policy.tools
    get_lead = policy.tools["get_lead"]
    assert "Email" in get_lead.allow_fields
    assert get_lead.redact_pii is not None
    assert get_lead.redact_pii.mode == "hash"
    assert get_lead.strip_metadata is True  # inherited from defaults


def test_load_policy_orders_default():
    policy = load_policy(POLICIES_DIR / "orders.default.yaml")
    assert policy.connector == "orders"
    get_order = policy.policy_for("get_order")
    assert "tracking_number" in get_order.allow_fields
    assert get_order.block_write_actions is True   # inherited from defaults


def test_load_policy_unknown_tool_falls_back_to_defaults():
    policy = load_policy(POLICIES_DIR / "orders.default.yaml")
    pol = policy.policy_for("undefined_tool")
    # No allow_fields, but defaults still apply.
    assert pol.allow_fields is None
    assert pol.strip_metadata is True
    assert pol.block_write_actions is True


# ---------------------------------------------------------------------------
# End-to-end on a Salesforce-shaped payload
# ---------------------------------------------------------------------------


def test_end_to_end_salesforce_lead_shrinks_payload():
    raw = {
        "Id": "00Q1aB",
        "FirstName": "Jane",
        "LastName": "Doe",
        "Email": "jane@example.com",
        "Phone": "+49 30 1234567",
        "Company": "ExampleCo",
        "Title": "VP Eng",
        "Status": "Working",
        "LeadSource": "Web",
        "OwnerId": "005xx",
        "Rating": "Hot",
        # Stuff the agent does not need:
        "Description": "lorem ipsum " * 200,
        "AnnualRevenue": 99_000_000,
        "CreatedDate": "2025-01-01T00:00:00Z",
        "LastModifiedDate": "2025-09-01T00:00:00Z",
        "SystemModstamp": "2025-09-01T00:00:00Z",
        "attributes": {"type": "Lead", "url": "/services/data/v59.0/sobjects/Lead/00Q1aB"},
        "Custom_Field_1__c": "secret",
        "InternalScore__c": 0.42,
    }
    policy = load_policy(POLICIES_DIR / "salesforce.default.yaml").policy_for("get_lead")
    shaped = apply(raw, policy)

    # Whitelisted fields present:
    for f in (
        "Id",
        "FirstName",
        "LastName",
        "Company",
        "Title",
        "Status",
        "LeadSource",
        "OwnerId",
        "Rating",
    ):
        assert f in shaped, f"missing {f}"

    # Email / Phone redacted, not removed:
    assert shaped["Email"].startswith("sha256:")
    assert shaped["Phone"].startswith("sha256:")

    # Crud gone:
    for f in (
        "Description",
        "AnnualRevenue",
        "CreatedDate",
        "LastModifiedDate",
        "SystemModstamp",
        "attributes",
        "Custom_Field_1__c",
        "InternalScore__c",
    ):
        assert f not in shaped, f"{f} should have been removed"
