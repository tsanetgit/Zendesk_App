"""rotate-inbound-webhook.py must keep its make-before-break call sequence.

tsanetgit/Zendesk_App#101 moves this script to the v2 webhook API. These tests
pin the current sequence against a fake TSANet and ZIS, so that move shows up
as test changes. Nothing here touches the network.
"""
import importlib.util
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TS = "https://tsanet.example"
ZIS = "https://sub.zendesk.com/api/services/zis/inbound_webhooks/generic/tsanet_connect"
OLD_PATH = "/api/services/zis/inbound_webhooks/generic/ingest/old"
NEW_PATH = "/api/services/zis/inbound_webhooks/generic/ingest/new"


def _load():
    spec = importlib.util.spec_from_file_location(
        "rotate", ROOT / "scripts" / "rotate-inbound-webhook.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(monkeypatch, tmp_path, *argv, verify_status=200, sub_status=200):
    mod = _load()
    calls, bodies = [], {}

    def fake(method, url, bearer=None, basic=None, body=None, **kw):
        calls.append((method, url))
        if method == "POST":
            bodies[url] = (body, basic)
        if method == "GET" and url == f"{ZIS}/old-uuid":
            return 200, {"path": OLD_PATH}
        if method == "GET" and url == f"{TS}/v1/webhooks":
            return 200, [{"id": 7, "callbackUrl": "https://sub.zendesk.com" + OLD_PATH,
                          "eventTypes": ["collaboration-request.created"]}]
        if method == "POST" and url == ZIS:
            return 201, {"uuid": "new-uuid", "path": NEW_PATH,
                         "username": "u", "password": "p"}
        if method == "POST" and url == f"{TS}/v1/webhooks":
            return sub_status, {"id": 8}
        if method == "POST" and url.endswith(NEW_PATH):
            return verify_status, {}
        return 204, {}

    monkeypatch.setattr(mod, "request", fake)
    for k, v in {"ZENDESK_SUBDOMAIN": "sub", "ZIS_TOKEN": "z",
                 "TSANET_HOST": "tsanet.example", "TSANET_TOKEN": "t",
                 "OLD_WEBHOOK_UUID": "old-uuid"}.items():
        monkeypatch.setenv(k, v)
    out = tmp_path / "creds.json"
    monkeypatch.setattr("sys.argv", ["rotate", "--out", str(out), *argv])
    return mod, calls, out, bodies


def test_rotation_creates_verifies_then_retires(monkeypatch, tmp_path):
    mod, calls, out, bodies = _run(monkeypatch, tmp_path)
    mod.main()
    assert calls == [
        ("GET", f"{ZIS}/old-uuid"),
        ("GET", f"{TS}/v1/webhooks"),
        ("POST", ZIS),
        ("POST", f"{TS}/v1/webhooks"),
        ("POST", "https://sub.zendesk.com" + NEW_PATH),
        ("DELETE", f"{TS}/v1/webhooks/7"),
        ("DELETE", f"{ZIS}/old-uuid"),
    ]
    creds = json.loads(out.read_text())
    assert creds["uuid"] == "new-uuid"
    assert out.stat().st_mode & 0o777 == 0o600
    sub_body, _ = bodies[f"{TS}/v1/webhooks"]
    assert sub_body["callbackUrl"].endswith(NEW_PATH)
    # The replacement keeps the old subscription's event types.
    assert sub_body["eventTypes"] == ["collaboration-request.created"]
    assert sub_body["callbackAuth"] == {"type": "BASIC", "username": "u",
                                        "password": "p"}
    probe, basic = bodies["https://sub.zendesk.com" + NEW_PATH]
    assert basic == "u:p"
    assert probe["requestToken"] == "rotation-verify-synthetic"
    # Not a creation event, so the flow can't turn the probe into a ticket.
    assert probe["eventType"] == "note.created"
    assert probe["timestamp"] == creds["rotated_at"]


def test_failed_subscription_create_deletes_only_the_new_webhook(
        monkeypatch, tmp_path):
    mod, calls, out, _ = _run(monkeypatch, tmp_path, sub_status=500)
    with pytest.raises(SystemExit):
        mod.main()
    assert calls[-1] == ("DELETE", f"{ZIS}/new-uuid")
    assert not [c for c in calls if c[0] == "DELETE" and c[1].startswith(TS)]
    assert not out.exists()


def test_failed_verify_rolls_back_only_the_new_halves(monkeypatch, tmp_path):
    mod, calls, out, _ = _run(monkeypatch, tmp_path, verify_status=401)
    with pytest.raises(SystemExit):
        mod.main()
    assert calls[-2:] == [("DELETE", f"{TS}/v1/webhooks/8"),
                          ("DELETE", f"{ZIS}/new-uuid")]
    assert ("DELETE", f"{TS}/v1/webhooks/7") not in calls
    assert not out.exists()


def test_dry_run_only_reads(monkeypatch, tmp_path):
    mod, calls, out, _ = _run(monkeypatch, tmp_path, "--dry-run")
    mod.main()
    assert [m for m, _ in calls] == ["GET", "GET"]
    assert not out.exists()
