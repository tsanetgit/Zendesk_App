"""rotate-inbound-webhook.py must keep its make-before-break call sequence.

tsanetgit/Zendesk_App#101 moved this script to the v2 webhook API. These tests
pin the sequence against a fake TSANet and ZIS, and pin that each new refusal
happens before anything changes. Nothing here touches the network.
"""
import datetime
import importlib.util
import json
import pathlib
import urllib.parse

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
LIST = f"{TS}/v2/collaboration-requests/list?"
TICKETS = f"{ZD}/api/v2/tickets.json?"
NOW = datetime.datetime(2026, 11, 19, 12, 0, 0)
# The fakes return case and ticket content; none of it may be printed.
SECRET = "CASE-CONTENT-MUST-NOT-LEAK"
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
         old_uuid="old-uuid", read_token=None, cases=(), tickets=None,
         read_status=200):
    mod = _load()
    # Default: before the v1 sunset, whatever today's date is.
    monkeypatch.setattr(mod, "V1_SUNSET", sunset)
    monkeypatch.setattr(mod, "utc_now", lambda offset=0: (
        NOW + datetime.timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    sleeps = []
    monkeypatch.setattr(mod.time, "sleep", sleeps.append)
    calls, bodies = [], {}
    tickets = tickets or {}

    def fake(method, url, bearer=None, basic=None, body=None, **kw):
        calls.append((method, url))
        if method == "POST":
            bodies[url] = (body, basic)
        if method == "GET" and url.startswith(LIST):
            return 200, [dict(c, description=SECRET) for c in cases]
        if method == "GET" and url.startswith(TICKETS):
            if bearer != "r":
                return 401, {}
            tok = urllib.parse.parse_qs(url.split("?", 1)[1])["external_id"][0]
            if tok.startswith("rotation-preflight-"):
                return 200, {"tickets": [], "count": 0}
            ids = tickets.get(tok, [])
            return read_status, {"tickets": [{"id": i, "subject": SECRET}
                                             for i in ids], "count": len(ids)}
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
                 "OLD_WEBHOOK_UUID": old_uuid,
                 "ZENDESK_READ_TOKEN": read_token}.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    out = tmp_path / "creds.json"
    monkeypatch.setattr("sys.argv", ["rotate", "--out", str(out), "--findings",
                                     str(tmp_path / "findings.json"), *argv])
    mod.sleeps = sleeps
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
    # Deleting the old webhook closed the window, so it has an end.
    assert json.loads(out.read_text())["swap_window"] == {
        "since": "2026-11-19T11:55:00Z", "until": "2026-11-19T12:01:00Z"}


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


# ── Duplicate-ticket sweep ──────────────────────────────────────────────────
# The window is NOW - 300 s to NOW + 60 s: 11:55:00Z to 12:01:00Z.
IN, IN2, IN3 = (f"2026-11-19T11:5{m}:30.123456Z" for m in (6, 7, 8))
CASES = [
    {"id": 1, "token": "dup", "createdAt": IN},
    {"id": 2, "token": "one", "createdAt": IN2},
    {"id": 3, "token": "none", "createdAt": IN3},
    {"id": 4, "token": "late", "createdAt": "2026-11-19T12:01:00.5Z"},
    {"id": 5, "token": "early", "createdAt": "2026-11-19T11:54:59.9Z"},
    {"id": 6},   # no token or createdAt: skipped with a warning
]
TICKETS_BY_TOKEN = {"dup": [41, 42], "one": [43], "late": [44, 45],
                    "early": [46, 47]}
SWEEP = ("--sweep", "--since", "2026-11-19T11:55:00Z",
         "--until", "2026-11-19T12:01:00Z")


def test_sweep_reports_only_duplicates_inside_the_window(
        monkeypatch, tmp_path, capsys):
    mod, calls, out, _ = _run(monkeypatch, tmp_path, *SWEEP, read_token="r",
                              cases=CASES, tickets=TICKETS_BY_TOKEN)
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 3
    printed = capsys.readouterr().out
    assert "DUPLICATE: case 1 (token dup) has tickets 41, 42" in printed
    # 12:01:00.5 is after the window by half a second: compared as times.
    assert "case 4" not in printed and "case 5" not in printed
    assert "3 case(s), 1 with one ticket, 1 with none yet, 1 with more" in printed
    findings = (tmp_path / "findings.json").read_text()
    assert json.loads(findings)["duplicates"] == [
        {"case": 1, "token": "dup", "created": IN, "tickets": [41, 42]}]
    assert SECRET not in printed + findings
    # A sweep never writes, and never touches the credentials file.
    assert {m for m, _ in calls} == {"GET"} and not out.exists()


def test_sweep_with_no_duplicates_exits_cleanly(monkeypatch, tmp_path):
    mod, _, _, _ = _run(monkeypatch, tmp_path, *SWEEP, read_token="r",
                        cases=CASES[1:3], tickets=TICKETS_BY_TOKEN)
    mod.main()


def test_rotation_sweeps_after_the_wait_and_prints_the_rerun(
        monkeypatch, tmp_path, capsys):
    mod, calls, out, _ = _run(monkeypatch, tmp_path, read_token="r",
                              cases=CASES, tickets=TICKETS_BY_TOKEN)
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 3
    assert mod.sleeps == [120]
    window = json.loads(out.read_text())["swap_window"]
    assert window == {"since": "2026-11-19T11:55:00Z",
                      "until": "2026-11-19T12:01:00Z"}
    # The sweep runs after every write, and the store line comes before it.
    first_list = calls.index(next(c for c in calls if c[1].startswith(LIST)))
    assert all(m == "GET" for m, _ in calls[first_list:])
    printed = capsys.readouterr().out
    assert printed.index("rotation complete") < printed.index("waiting 120s")
    assert "DUPLICATE: case 1" in printed
    assert printed.rstrip().endswith(
        "--sweep --since 2026-11-19T11:55:00Z --until 2026-11-19T12:01:00Z")
    assert SECRET not in printed


@pytest.mark.parametrize("argv", [(), ("--dry-run",)])
def test_rejected_read_token_stops_before_any_change(monkeypatch, tmp_path,
                                                     argv):
    mod, calls, out, _ = _run(monkeypatch, tmp_path, *argv, read_token="bad")
    with pytest.raises(SystemExit):
        mod.main()
    assert {m for m, _ in calls} == {"GET"}
    assert not out.exists()


def test_missing_read_token_warns_skips_and_prints_the_command(
        monkeypatch, tmp_path, capsys):
    mod, calls, _, _ = _run(monkeypatch, tmp_path)
    mod.main()
    printed = capsys.readouterr().out
    assert printed.index("ZENDESK_READ_TOKEN is not set") < printed.index(
        "new webhook:")
    assert "SKIPPED (ZENDESK_READ_TOKEN not set)" in printed
    assert "--sweep --since 2026-11-19T11:55:00Z" in printed
    assert mod.sleeps == [] and not [c for c in calls if c[1].startswith(LIST)]


def test_open_window_is_reported_open_and_still_swept(
        monkeypatch, tmp_path, capsys):
    """Old subscription not deleted and no old uuid: both still deliver."""
    mod, _, out, _ = _run(monkeypatch, tmp_path, "--old-ingest-path", OLD_PATH,
                          old_uuid=None, del_status=500, read_token="r",
                          cases=CASES, tickets=TICKETS_BY_TOKEN)
    with pytest.raises(SystemExit) as exc:
        mod.main()
    # A failed retire step outranks duplicates.
    assert exc.value.code == 1
    assert json.loads(out.read_text())["swap_window"]["until"] is None
    printed = capsys.readouterr().out
    assert "keep getting two tickets" in printed
    assert "--until <when you delete subscription 7>" in printed
    assert "DUPLICATE: case 1" in printed


def test_a_failed_post_swap_sweep_only_warns(monkeypatch, tmp_path, capsys):
    mod, _, _, _ = _run(monkeypatch, tmp_path, read_token="r", cases=CASES,
                        tickets=TICKETS_BY_TOKEN, read_status=500)
    mod.main()   # no retire failure, no findings: exit 0
    captured = capsys.readouterr()
    assert "cannot look up tickets" in captured.err
    assert "--sweep --since 2026-11-19T11:55:00Z" in captured.out.splitlines()[-1]


def test_an_interrupted_window_write_keeps_the_credentials(
        monkeypatch, tmp_path):
    mod, _, out, _ = _run(monkeypatch, tmp_path)
    dump = json.dump

    def disk_full(obj, f, **kw):
        if "swap_window" in obj:
            raise OSError("No space left on device")
        dump(obj, f, **kw)

    monkeypatch.setattr(mod.json, "dump", disk_full)
    with pytest.raises(OSError):
        mod.main()
    # The new subscription is live by now; the password must survive.
    assert json.loads(out.read_text())["password"] == "p"


@pytest.mark.parametrize("value, expected", [
    ("2026-11-19T12:00:00Z", NOW),
    ("2026-11-19T07:00:00-05:00", NOW),
    ("2026-11-19T13:30:00.5+01:30", NOW + datetime.timedelta(seconds=0.5)),
    ("2026-11-19T12:00:00.1234567Z", NOW + datetime.timedelta(
        microseconds=123456)),
])
def test_parse_utc_reads_offsets_and_fractions(value, expected):
    assert _load().parse_utc(value) == expected


@pytest.mark.parametrize("value", [
    "2026-11-19T12:00:00", "2026-11-19T12:00:00Zjunk", "2026-11-19T12:00:00.Z",
    "2026-11-19 12:00:00Z", "2026-11-19T12:00:00+0500"])
def test_parse_utc_refuses_what_it_cannot_read(value):
    with pytest.raises(ValueError):
        _load().parse_utc(value)


def test_sweep_window_with_an_offset_covers_the_same_instants(
        monkeypatch, tmp_path, capsys):
    mod, _, _, _ = _run(monkeypatch, tmp_path, "--sweep",
                        "--since", "2026-11-19T06:55:00-05:00",
                        "--until", "2026-11-19T07:01:00-05:00",
                        read_token="r", cases=CASES, tickets=TICKETS_BY_TOKEN)
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 3
    assert "DUPLICATE: case 1 (token dup)" in capsys.readouterr().out


def test_an_unreadable_created_at_is_skipped_not_fatal(
        monkeypatch, tmp_path, capsys):
    cases = [*CASES, {"id": 9, "token": "dup", "createdAt": "yesterday"}]
    mod, _, _, _ = _run(monkeypatch, tmp_path, read_token="r", cases=cases,
                        tickets=TICKETS_BY_TOKEN)
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 3
    captured = capsys.readouterr()
    assert "case 9 has no token or no readable createdAt" in captured.err
    assert "--sweep --since 2026-11-19T11:55:00Z" in captured.out.splitlines()[-1]


@pytest.mark.parametrize("argv", [
    ("--sweep-wait", "-1"),
    ("--sweep", "--since", "2026-11-19T12:01:00Z",
     "--until", "2026-11-19T11:55:00Z"),
])
def test_bad_sweep_arguments_stop_before_any_call(monkeypatch, tmp_path, argv):
    mod, calls, out, _ = _run(monkeypatch, tmp_path, *argv, read_token="r")
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 1
    assert calls == [] and not out.exists()
