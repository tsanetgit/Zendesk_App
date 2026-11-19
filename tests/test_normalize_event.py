"""flow_handle_ping must accept v1 flat and v2 CloudEvents deliveries.

tsanetgit/Zendesk_App#101: v1 webhooks sunset on 2027-01-01. While members
move their subscriptions, one bundle must handle both shapes, and only the
creation event may create a ticket (the allowlist from #86).

The jq tests run the bundle's real NormalizeEvent expression through the jq
CLI. ZIS runs its own jq engine; a delivery to a test instance is still the
check that it evaluates the same there.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
BUNDLE = ROOT / "zis" / "tsanet_connect_bundle.json"
CREATED = "collaboration-request.created"
V2 = "org.tsanet.connect.collaboration-request."


def _flow():
    bundle = json.loads(BUNDLE.read_text())
    return bundle["resources"]["flow_handle_ping"]["properties"]["definition"]


def _normalize(payload):
    jq = shutil.which("jq")
    assert jq, "the jq CLI is required for these tests"
    expr = _flow()["States"]["NormalizeEvent"]["Parameters"]["expr"]
    out = subprocess.run([jq, "-c", expr], input=json.dumps({"input": payload}),
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def _v2(kind, data):
    return {"specversion": "1.0", "id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
            "source": "https://connect2.example", "type": V2 + kind,
            "time": "2026-11-18T00:00:00Z", "datacontenttype": "application/json",
            "subscriptionid": 42, "data": data}


# PROVISIONAL(tsanetgit/Zendesk_App#101): the v1 arm of NormalizeEvent and the
# v1 cases here exist only while members still have v1 subscriptions. Once every
# known install is on v2, drop `.input.eventType` / `.input.requestToken` from
# the expression and these cases. The bundle is JSON, so the marker lives here.
def test_v1_creation_event():
    out = _normalize({"eventType": CREATED, "requestToken": "tok1",
                      "timestamp": "2026-11-18T00:00:00Z"})
    assert out == {"token": "tok1", "event": CREATED, "ok": "yes"}


def test_v2_creation_event():
    out = _normalize(_v2("created", {"requestToken": "tok2"}))
    assert out == {"token": "tok2", "event": CREATED, "ok": "yes"}


@pytest.mark.parametrize("payload", [
    {"eventType": "note.created", "requestToken": "tok", "noteToken": "n1"},
    _v2("closed", {"requestToken": "tok"}),
    _v2("note.created", {"requestToken": "tok", "noteToken": "n1"}),
    _v2("response.created", {"requestToken": "tok"}),
    _v2("response.updated", {"requestToken": "tok"}),
])
def test_no_other_event_is_the_creation_event(payload):
    out = _normalize(payload)
    assert out["event"] != CREATED
    assert out["ok"] == "yes" and out["token"] == "tok"


@pytest.mark.parametrize("payload", [
    {"eventType": CREATED},
    {"eventType": CREATED, "requestToken": ""},
    {"eventType": CREATED, "requestToken": 123},
    _v2("created", {}),
    _v2("created", {"requestToken": None}),
    {},
])
def test_missing_token_is_not_ok(payload):
    assert _normalize(payload)["ok"] == "no"


def test_flow_routes_through_the_normalized_event():
    flow = _flow()
    states = flow["States"]
    assert flow["StartAt"] == "NormalizeEvent"
    assert states["NormalizeEvent"]["ResultPath"] == "$.evt"
    assert states["NormalizeEvent"]["Next"] == "GuardEventToken"
    guard = states["GuardEventToken"]
    assert guard["Choices"] == [{"Variable": "$.evt.ok", "StringEquals": "yes",
                                 "Next": "GetCollaboration"}]
    assert guard["Default"] == "NoOp"
    assert states["GetCollaboration"]["Parameters"] == {"requestToken.$": "$.evt.token"}
    create = states["GuardCreate"]
    assert create["Choices"] == [{"Variable": "$.evt.event", "StringEquals": CREATED,
                                  "Next": "BuildSubmitter"}]
    assert create["Default"] == "NoOp"
    assert "$.input." not in json.dumps(flow)
