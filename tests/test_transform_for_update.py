"""flow_handle_ping's TransformForUpdate must apply the sidebar's field rules.

tsanetgit/Zendesk_App#235: the ZIS update path wrote TSANet Partner as the
receiving company and TSANet Respond By whenever a deadline existed, while the
sidebar (zaf-build/assets/main.js, syncStatusToTicket) writes the submitting
company on INBOUND cases and clears Respond By once the case is acknowledged.
Each overwrote the other on every partner reply.

These tests run the bundle's real jq expression through the jq CLI, so they
pin the logic. ZIS runs its own jq engine; a deploy to a test instance is
still the check that the expression evaluates the same there.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
BUNDLE = ROOT / "zis" / "tsanet_connect_bundle.json"

# Field-id placeholders, as shipped. deploy.js substitutes them textually.
STATUS, PARTNER, RESPOND_BY = 1234567892, 1234567893, 1234567894


def _states():
    bundle = json.loads(BUNDLE.read_text())
    return bundle["resources"]["flow_handle_ping"]["properties"]["definition"]["States"]


def _run(collab, ticket):
    jq = shutil.which("jq")
    assert jq, "the jq CLI is required for these tests"
    expr = _states()["TransformForUpdate"]["Parameters"]["expr"]
    out = subprocess.run(
        [jq, "-c", expr],
        input=json.dumps({"collab": collab, "zd": {"ticket": ticket}}),
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


def _ticket(status, partner, respond_by, tags=("tsanet_inbound", "tsanet_updated")):
    return {
        "tags": list(tags),
        "updated_at": "2026-10-08T00:00:00Z",
        "custom_fields": [
            {"id": STATUS, "value": status},
            {"id": PARTNER, "value": partner},
            {"id": RESPOND_BY, "value": respond_by},
        ],
    }


def _collab(direction, responded, status="OPEN", respond_by="2026-10-09T15:00:00Z"):
    return {
        "status": status,
        "direction": direction,
        "submitCompanyName": "Submitter Co",
        "receiveCompanyName": "Receiver Co",
        "responded": responded,
        "respondBy": respond_by,
    }


def _fields(result):
    return {f["id"]: f["value"] for f in result["body"]["ticket"].get("custom_fields", [])}


def test_inbound_partner_is_the_submitting_company_235():
    # The ticket holds what the old ZIS path wrote: the member's own company.
    r = _run(_collab("INBOUND", False),
             _ticket("tsanet_status_open", "Receiver Co", "2026-10-09"))
    assert _fields(r) == {PARTNER: "Submitter Co"}
    assert r["writes"] == 1


def test_outbound_partner_is_the_receiving_company_235():
    r = _run(_collab("OUTBOUND", False),
             _ticket("tsanet_status_open", "Receiver Co", "2026-10-09"))
    assert r["writes"] == 0
    assert "custom_fields" not in r["body"]["ticket"]


def test_respond_by_follows_the_sidebar_when_responded_is_absent_235():
    # syncStatusToTicket keeps the deadline only while responded === false, so
    # an absent value clears it. Matching that exactly is what stops the flip.
    collab = _collab("OUTBOUND", None)
    del collab["responded"]
    r = _run(collab, _ticket("tsanet_status_open", "Receiver Co", "2026-10-09"))
    assert _fields(r) == {RESPOND_BY: None}


def test_respond_by_clears_once_acknowledged_235():
    r = _run(_collab("OUTBOUND", True, status="ACCEPTED"),
             _ticket("tsanet_status_accepted", "Receiver Co", "2026-10-09"))
    assert _fields(r) == {RESPOND_BY: None}


def test_sidebar_state_is_left_alone_235():
    # Exactly what syncStatusToTicket leaves on an acknowledged inbound case.
    r = _run(_collab("INBOUND", True, status="ACCEPTED"),
             _ticket("tsanet_status_accepted", "Submitter Co", None))
    assert r["writes"] == 0
    assert "custom_fields" not in r["body"]["ticket"]


def test_null_and_empty_respond_by_are_equal_235():
    r = _run(_collab("OUTBOUND", None, respond_by=None),
             _ticket("tsanet_status_open", "Receiver Co", ""))
    assert r["writes"] == 0


def test_missing_tag_still_writes_tag_only_235():
    # tsanet_updated is a documented retention selector; keep applying it.
    r = _run(_collab("OUTBOUND", False),
             _ticket("tsanet_status_open", "Receiver Co", "2026-10-09", tags=("tsanet_outbound",)))
    assert r["writes"] == 1
    assert "custom_fields" not in r["body"]["ticket"]
    assert "tsanet_updated" in r["body"]["ticket"]["tags"]


def test_write_body_keeps_the_conflict_guard_235():
    r = _run(_collab("INBOUND", False, status="ACCEPTED"),
             _ticket("tsanet_status_open", "Submitter Co", "2026-10-09"))
    t = r["body"]["ticket"]
    assert _fields(r) == {STATUS: "tsanet_status_accepted"}
    assert t["safe_update"] is True and t["updated_stamp"] == "2026-10-08T00:00:00Z"


def test_choice_routes_no_change_to_noop_235():
    st = _states()
    assert st["TransformForUpdate"]["Next"] == "CheckForChanges"
    choice = st["CheckForChanges"]
    assert choice["Type"] == "Choice" and choice["Default"] == "NoOp"
    assert choice["Choices"] == [
        {"Variable": "$.xform.writes", "NumericGreaterThan": 0, "Next": "UpdateTicket"}
    ]
    assert st["UpdateTicket"]["Parameters"]["requestBody.$"] == "$.xform.body"


@pytest.mark.parametrize("placeholder", [STATUS, PARTNER, RESPOND_BY])
def test_field_placeholders_stay_literal_235(placeholder):
    # deploy.js replaces these as bare numbers; quoting one would break it.
    expr = _states()["TransformForUpdate"]["Parameters"]["expr"]
    assert f"id: {placeholder}," in expr
