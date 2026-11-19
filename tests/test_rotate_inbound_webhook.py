"""rotate-inbound-webhook.py must keep its make-before-break call sequence.

tsanetgit/Zendesk_App#101 moved this script to the v2 webhook API. These tests
pin the sequence against a fake TSANet and ZIS, and pin that each new refusal
happens before anything changes. Nothing here touches the network.
"""
import importlib.util
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TS = "https://tsanet.example"
V1_LIST = f"{TS}/v1/webhooks"  # audit-degrades: v1-webhooks until 2027-01-01 tracked-by tsanetgit/Zendesk_App#101
V2 = f"{TS}/v2/webhooks"
ZD = "https://sub.zendesk.com"
ZIS = ZD + "/api/services/zis/inbound_webhooks/generic/tsanet_connect"
BUNDLES = ZD + "/api/services/zis/registry/tsanet_connect/bundles"
OLD_PATH = "/api/services/zis/inbound_webhooks/generic/ingest/old"
NEW_PATH = "/api/services/zis/inbound_webhooks/generic/ingest/new"
V1_SUB = {"id": 7, "callbackUrl": ZD + OLD_PATH,
          "eventTypes": ["collaboration-request.created"],
          "caseDirections": ["INBOUND"]}


def _load():
    spec = importlib.util.spec_from_file_location(
        "rotate", ROOT / "scripts" / "rotate-inbound-webhook.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(monkeypatch, tmp_path, *argv, v1=(200, [V1_SUB]), v2=(200, []),
         start="NormalizeEvent", verify_status=200, sub_status=200,
         del_status=204, new_del_status=204, sunset="9999-12-31",
         old_uuid="old-uuid"):
    mod = _load()
    # Default: before the v1 sunset, whatever today's date is.
    monkeypatch.setattr(mod, "V1_SUNSET", sunset)
    calls, bodies = [], {}

    def fake(method, url, bearer=None, basic=None, body=None, **kw):
        calls.append((method, url))
        if method == "POST":
            bodies[url] = (body, basic)
        if method == "GET" and url == f"{ZIS}/old-uuid":
            return 200, {"path": OLD_PATH}
        if method == "GET" and url == V1_LIST:
            return v1
        if method == "GET" and url == V2:
            return v2
        if method == "GET" and url == BUNDLES:
            return 200, {"bundles": [{"uuid": "b1"}]}
        if method == "GET" and url == f"{BUNDLES}/b1":
            return 200, {"resources": {"flow_handle_ping": {"properties": {
                "definition": {"StartAt": start}}}}}
        if method == "POST" and url == ZIS:
            return 201, {"uuid": "new-uuid", "path": NEW_PATH,
                         "username": "u", "password": "p"}
        if method == "POST" and url == V2:
            return sub_status, {"id": 8}
        if method == "POST" and url.endswith(NEW_PATH):
            return verify_status, {}
        if method == "DELETE" and url.startswith(TS):
            return del_status, {}
        if method == "DELETE" and url == f"{ZIS}/new-uuid":
            return new_del_status, {}
        return 204, {}

    monkeypatch.setattr(mod, "request", fake)
    for k, v in {"ZENDESK_SUBDOMAIN": "sub", "ZIS_TOKEN": "z",
                 "TSANET_HOST": "tsanet.example", "TSANET_TOKEN": "t",
                 "OLD_WEBHOOK_UUID": old_uuid}.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    out = tmp_path / "creds.json"
    monkeypatch.setattr("sys.argv", ["rotate", "--out", str(out), *argv])
    return mod, calls, out, bodies


READS = [("GET", f"{ZIS}/old-uuid"), ("GET", V1_LIST), ("GET", V2),
         ("GET", BUNDLES), ("GET", f"{BUNDLES}/b1")]


def test_rotation_moves_a_v1_subscription_to_v2(monkeypatch, tmp_path):
    mod, calls, out, bodies = _run(monkeypatch, tmp_path)
    mod.main()
    assert calls == READS + [
        ("POST", ZIS),
        # Verified before any subscription points at the new URL.
        ("POST", ZD + NEW_PATH),
        ("POST", V2),
        ("DELETE", f"{TS}/v1/webhooks/7"),
        ("DELETE", f"{ZIS}/old-uuid"),
    ]
    creds = json.loads(out.read_text())
    assert creds["uuid"] == "new-uuid"
    assert out.stat().st_mode & 0o777 == 0o600
    sub_body, _ = bodies[V2]
    assert sub_body["callbackUrl"].endswith(NEW_PATH)
    # The old scope, under its v2 names.
    assert sub_body["eventTypes"] == [
        "org.tsanet.connect.collaboration-request.created"]
    assert sub_body["caseDirections"] == ["INBOUND"]
    assert "partnerCompanyIds" not in sub_body
    assert sub_body["callbackAuth"] == {"type": "BASIC", "username": "u",
                                        "password": "p"}
    probe, basic = bodies[ZD + NEW_PATH]
    assert basic == "u:p"
    # A v2 envelope with no token: the flow ends at NoOp, never at TSANet.
    assert probe["type"] == ("org.tsanet.connect.collaboration-request"
                             ".note.created")
    assert probe["data"] == {}
    assert probe["time"] == creds["rotated_at"]


def test_v1_subscription_with_no_filter_gets_both_types_written_out(
        monkeypatch, tmp_path):
    sub = dict(V1_SUB, eventTypes=None)
    mod, _, _, bodies = _run(monkeypatch, tmp_path, v1=(200, [sub]))
    mod.main()
    assert bodies[V2][0]["eventTypes"] == [
        "org.tsanet.connect.collaboration-request.created",
        "org.tsanet.connect.collaboration-request.note.created"]


def test_v2_subscription_keeps_its_scope(monkeypatch, tmp_path):
    sub = dict(V1_SUB, id=9, eventTypes=None, partnerCompanyIds=[42])
    mod, calls, _, bodies = _run(monkeypatch, tmp_path, v1=(200, []),
                                 v2=(200, [sub]), sub_status=201)
    mod.main()
    body = bodies[V2][0]
    # No filter on v2 means all five types; the rotation must not narrow it.
    assert "eventTypes" not in body
    assert body["partnerCompanyIds"] == [42]
    # The v1 path is the only delete, for v2 subscriptions too.
    assert ("DELETE", f"{TS}/v1/webhooks/9") in calls


def test_after_the_sunset_an_unreadable_v1_list_warns_and_uses_v2(
        monkeypatch, tmp_path, capsys):
    sub = dict(V1_SUB, id=9, eventTypes=[
        "org.tsanet.connect.collaboration-request.created"])
    mod, calls, _, bodies = _run(monkeypatch, tmp_path, v1=(404, {}),
                                 v2=(200, [sub]), sunset="2000-01-01")
    mod.main()
    assert "cannot list v1 subscriptions" in capsys.readouterr().err
    assert bodies[V2][0]["eventTypes"] == sub["eventTypes"]


def test_after_the_sunset_a_missing_subscription_points_at_the_runbook(
        monkeypatch, tmp_path, capsys):
    mod, calls, _, _ = _run(monkeypatch, tmp_path, "--subscription-id", "7",
                            v1=(404, {}), sunset="2000-01-01")
    with pytest.raises(SystemExit):
        mod.main()
    err = capsys.readouterr().err
    # Not "pass --subscription-id": the id was passed and can't be seen.
    assert "Manual fallback" in err and "Pass --subscription-id" not in err
    assert {m for m, _ in calls} == {"GET"}


def test_empty_filters_are_left_out(monkeypatch, tmp_path):
    sub = dict(V1_SUB, eventTypes=[], caseDirections=[], partnerCompanyIds=[])
    mod, _, _, bodies = _run(monkeypatch, tmp_path, v1=(200, [sub]))
    mod.main()
    body = bodies[V2][0]
    # Empty means no filter; an empty caseDirections fails v2's minItems: 1.
    assert "caseDirections" not in body and "partnerCompanyIds" not in body
    assert len(body["eventTypes"]) == 2


@pytest.mark.parametrize("argv", [(), ("--dry-run",)])
@pytest.mark.parametrize("case", [
    {"start": "GetCollaboration"},
    {"v1": (200, [dict(V1_SUB, eventTypes=["case.closed"])])},
    {"v2": (200, [dict(V1_SUB, id=9)])},
    {"v1": (200, [dict(V1_SUB, active=False)])},
    # Before the sunset a failed v1 list could hide a v1 duplicate of the
    # v2 subscription it would otherwise rotate.
    {"v1": (500, {}), "v2": (200, [dict(V1_SUB, id=9, eventTypes=None)])},
])
def test_refusals_change_nothing(monkeypatch, tmp_path, argv, case):
    """An older bundle, an unmappable v1 type, two subscriptions already on
    the old URL (duplicate tickets now), an inactive subscription, or a failed
    v1 list before the sunset stop the run before any write."""
    mod, calls, out, _ = _run(monkeypatch, tmp_path, *argv, **case)
    with pytest.raises(SystemExit):
        mod.main()
    assert {m for m, _ in calls} == {"GET"}
    assert not out.exists()


def test_duplicates_stop_even_with_a_subscription_id(monkeypatch, tmp_path):
    mod, calls, _, _ = _run(monkeypatch, tmp_path, "--subscription-id", "7",
                            v2=(200, [dict(V1_SUB, id=9)]))
    with pytest.raises(SystemExit):
        mod.main()
    assert {m for m, _ in calls} == {"GET"}


def test_failed_verify_deletes_only_the_new_webhook(monkeypatch, tmp_path):
    mod, calls, out, _ = _run(monkeypatch, tmp_path, verify_status=401)
    with pytest.raises(SystemExit):
        mod.main()
    assert calls[-1] == ("DELETE", f"{ZIS}/new-uuid")
    assert ("POST", V2) not in calls
    assert not [c for c in calls if c[0] == "DELETE" and c[1].startswith(TS)]
    assert not out.exists()


def test_failed_subscription_create_deletes_only_the_new_webhook(
        monkeypatch, tmp_path):
    mod, calls, out, _ = _run(monkeypatch, tmp_path, sub_status=500)
    with pytest.raises(SystemExit):
        mod.main()
    assert calls[-1] == ("DELETE", f"{ZIS}/new-uuid")
    assert not [c for c in calls if c[0] == "DELETE" and c[1].startswith(TS)]
    assert not out.exists()


def test_failed_old_subscription_delete_still_retires_webhook_and_fails(
        monkeypatch, tmp_path):
    mod, calls, out, _ = _run(monkeypatch, tmp_path, del_status=500)
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 1
    # Deleting the old webhook still cuts the old subscription's deliveries.
    assert calls[-1] == ("DELETE", f"{ZIS}/old-uuid")
    # The new pipe is live, so its credentials must survive the failure.
    assert out.exists()


def test_failed_rollback_keeps_the_credentials_file(monkeypatch, tmp_path):
    mod, calls, out, _ = _run(monkeypatch, tmp_path, verify_status=401,
                              new_del_status=0)
    with pytest.raises(SystemExit):
        mod.main()
    # The file is the only record of a webhook that may still be live.
    assert out.exists()


def test_legacy_path_says_the_old_subscription_still_delivers(
        monkeypatch, tmp_path, capsys):
    mod, _, _, _ = _run(monkeypatch, tmp_path, "--old-ingest-path", OLD_PATH,
                        old_uuid=None, del_status=500)
    with pytest.raises(SystemExit):
        mod.main()
    out = capsys.readouterr().out
    assert "still delivers to it" in out and "nothing routes to it" not in out


def test_no_response_is_a_status_not_a_traceback(monkeypatch):
    mod = _load()

    def boom(*a, **kw):
        raise mod.urllib.error.URLError("timed out")
    monkeypatch.setattr(mod.urllib.request, "urlopen", boom)
    status, body = mod.request("DELETE", f"{TS}/v1/webhooks/7", bearer="t")
    assert status == 0 and "timed out" in body["error"]


def test_dry_run_only_reads(monkeypatch, tmp_path):
    mod, calls, out, _ = _run(monkeypatch, tmp_path, "--dry-run")
    mod.main()
    assert calls == READS
    assert not out.exists()
