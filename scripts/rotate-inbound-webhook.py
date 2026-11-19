#!/usr/bin/env python3
"""Rotate the ZIS inbound-webhook Basic credential (make-before-break).

Replaces the TSANet -> ZIS ingest webhook and its Basic credential with a
fresh one, verifies the new path accepts an authenticated delivery, registers
a v2 TSANet webhook subscription at it with the old one's scope, then deletes
the old subscription (v1 or v2) and old ZIS webhook. Needs app 1.0.72 or
later deployed (checked first). The old credential is fully revoked
at the end: ZIS inbound webhooks CAN be deleted (DELETE
.../generic/{integration}/{uuid}, validated 2026-07-22) even though the
Zendesk docs do not list the operation.

Rotation always changes the ingest URL. That is inherent to ZIS (credentials
cannot be regenerated in place); it is why the TSANet subscription must be
re-created rather than updated.

Required environment:
  ZENDESK_SUBDOMAIN   your Zendesk subdomain (the X in X.zendesk.com)
  ZIS_TOKEN           ZIS OAuth bearer token (scopes: zis:read zis:write)
  TSANET_HOST         TSANet API host, e.g. connect2.tsanet.org
  TSANET_TOKEN        TSANet API bearer token (Entra client-credentials)
  OLD_WEBHOOK_UUID    uuid of the current ZIS inbound webhook (the `uuid`
                      field from its create response, NOT the `id` ULID).
                      Optional for legacy installs that never recorded it:
                      pass --old-ingest-path instead, and accept that the
                      old webhook cannot be deleted (its credential stays
                      valid; ZIS has no list API to recover the uuid).
  ZENDESK_READ_TOKEN  optional Zendesk OAuth token with the `read` scope, for
                      the duplicate-ticket sweep. Without it the sweep is
                      skipped and the command to run it later is printed.

Usage:
  rotate-inbound-webhook.py [--dry-run] [--subscription-id N]
                            [--integration NAME] [--out FILE]
                            [--sweep-wait SECONDS] [--findings FILE]
  rotate-inbound-webhook.py --sweep --since TIME [--until TIME]

While both subscriptions are live, a new case can get two tickets. After a
rotation the script waits, then looks up every case TSANet created in that
window and reports any with more than one ticket. It reports only; it never
changes a ticket. --sweep re-runs that check on its own.

Exit codes: 0 done; 1 error, or a retire step failed; 3 duplicates found.

New credentials are written to --out (default: zis-webhook-credentials.json,
chmod 600). They are never printed. Store the file contents in your secret
manager, then delete the file. Record the new webhook's uuid: ZIS has no
list operation, so a lost uuid cannot be recovered or deleted later.
"""
import argparse
import base64
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

INTEGRATION_DEFAULT = "tsanet_connect"
V1_SUNSET = "2027-01-01"   # GET and POST /v1/webhooks stop working
FINDINGS_DEFAULT = "duplicate-tickets.json"
# The sweep window is the swap's, widened: before, by TSANet's delivery
# retries (4 more attempts, 10 s doubling, about 150 s in all, per 3.2.5);
# after, by clock skew between this host and TSANet.
SWEEP_PAD_BEFORE = 300
SWEEP_PAD_AFTER = 60

# v1 event type -> its v2 name (Connect-API-Code 3.2.5, WebhookEventType and
# WebhookEventTypeV2). v2's other three types have no v1 counterpart.
V1_TO_V2_EVENT_TYPES = {
    "collaboration-request.created":
        "org.tsanet.connect.collaboration-request.created",
    "note.created": "org.tsanet.connect.collaboration-request.note.created",
}


def utc_now(offset=0):
    t = datetime.datetime.now(datetime.timezone.utc)
    return (t + datetime.timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value):
    """UTC "YYYY-MM-DDTHH:MM:SS[.fraction]Z"; compare these, never strings."""
    whole = datetime.datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
    digits = value[20:].rstrip("Z") if value[19:20] == "." else ""
    return whole + datetime.timedelta(microseconds=int((digits + "000000")[:6]))


def require_env(*names):
    env = {}
    for var in names:
        val = os.environ.get(var)
        if not val:
            die(f"missing required environment variable {var} (see --help)")
        env[var] = val
    return env


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def request(method, url, bearer=None, basic=None, body=None,
            content_type="application/json"):
    req = urllib.request.Request(url, method=method)
    # TSANet sits behind Cloudflare, which rejects python-urllib's default
    # User-Agent (error 1010). Any recognizable client string passes.
    req.add_header("User-Agent", "tsanet-zendesk-rotate/1.0")
    if bearer:
        req.add_header("Authorization", f"Bearer {bearer}")
    elif basic:
        req.add_header("Authorization",
                       "Basic " + base64.b64encode(basic.encode()).decode())
    data = None
    if body is not None:
        req.add_header("Content-Type", content_type)
        data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(req, data=data) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read() or b"{}")
        except Exception:
            payload = {}
        return e.code, payload
    except OSError as e:
        # No response (timeout, DNS, refused). Status 0 sends every caller
        # down its failure path instead of out through a traceback.
        return 0, {"error": str(e)}


def find_subscription(ts, token, subscription_id, old_path):
    """Return (subscription, "v1" or "v2") for the one to rotate, or exit.

    v1 is listed first, and an id found there is v1: a v2 subscription does
    not appear in the v1 list. Before the v1 sunset a failed v1 list stops
    the run, since skipping it could miss a v1 duplicate; after the sunset
    the list is gone, so it is a warning.
    """
    found = {}
    status, subs = request("GET", f"{ts}/v1/webhooks", bearer=token)  # audit-degrades: v1-webhooks until 2027-01-01 tracked-by tsanetgit/Zendesk_App#101
    v1_listed = status == 200
    if v1_listed:
        found.update((s["id"], (s, "v1")) for s in subs)
    elif time.strftime("%Y-%m-%d", time.gmtime()) < V1_SUNSET:
        die(f"cannot list v1 subscriptions (HTTP {status})")
    else:
        print(f"warning: cannot list v1 subscriptions (HTTP {status}); "
              "looking on v2 only", file=sys.stderr)
    status, subs = request("GET", f"{ts}/v2/webhooks", bearer=token)
    if status != 200:
        die(f"cannot list TSANet v2 subscriptions (HTTP {status})")
    for s in subs:
        found.setdefault(s["id"], (s, "v2"))
    on_url = [m for m in found.values() if m[0].get("callbackUrl", "")
              .rstrip("/").endswith(old_path.rstrip("/"))]
    if len(on_url) > 1:
        die(f"{len(on_url)} subscriptions point at the old ingest URL, so each "
            "case is getting more than one ticket now. Delete the extra "
            "subscriptions before rotating.")
    if subscription_id:
        matches = [found[subscription_id]] if subscription_id in found else []
    else:
        matches = on_url
    if not matches and not v1_listed:
        die("no matching subscription on v2, and v1 subscriptions can't be "
            "listed after the sunset. Move a v1 subscription by hand: see "
            "Manual fallback in ZIS_Rotation_Runbook.md.")
    if len(matches) != 1:
        die(f"expected exactly 1 matching subscription, found {len(matches)}. "
            "Pass --subscription-id explicitly.")
    return matches[0]


def check_bundle(zd, integration, zis_token):
    """Exit unless the deployed flow_handle_ping can read v2 deliveries.

    From app 1.0.72 the flow starts at NormalizeEvent, which reads either
    payload shape. An older flow reads only v1's flat requestToken and fails
    every v2 delivery. A stale bundle could be the one its job spec runs, so
    every deployed bundle that defines the flow must be the new one.
    """
    bundles = f"{zd}/api/services/zis/registry/{integration}/bundles"
    status, listing = request("GET", bundles, bearer=zis_token)
    if status != 200:
        die(f"cannot list deployed ZIS bundles (HTTP {status})")
    flows = 0
    for b in listing.get("bundles") or []:
        status, bundle = request("GET", f"{bundles}/{b['uuid']}",
                                 bearer=zis_token)
        if status != 200:
            die(f"cannot read deployed bundle {b['uuid']} (HTTP {status})")
        flow = (bundle.get("resources") or {}).get("flow_handle_ping")
        if not flow:
            continue
        flows += 1
        start = flow.get("properties", {}).get("definition", {}).get("StartAt")
        if start != "NormalizeEvent":
            die(f"deployed bundle {b['uuid']} starts flow_handle_ping at "
                f"{start!r}, so it cannot read v2 deliveries. Deploy app "
                "1.0.72 or later first.")
    if not flows:
        die("no deployed bundle defines flow_handle_ping. Deploy app 1.0.72 "
            "or later first.")


def replacement_event_types(sub, version):
    """The v2 eventTypes for the replacement, keeping the old scope."""
    types = sub.get("eventTypes")
    if version == "v2":
        return types
    unknown = [t for t in types or [] if t not in V1_TO_V2_EVENT_TYPES]
    if unknown:
        die(f"subscription {sub['id']} has v1 event types with no v2 "
            f"mapping: {unknown}")
    # No v1 filter means every v1 type. Written out, because on v2 no filter
    # means all five v2 types, three of which v1 never delivered.
    return [V1_TO_V2_EVENT_TYPES[t] for t in types or V1_TO_V2_EVENT_TYPES]


def delete_subscription(ts, token, sub_id):
    """Delete a TSANet subscription, v1 or v2; return the HTTP status.

    The v1 path is the only delete there is, and it is not deprecated.
    """
    status, _ = request("DELETE", f"{ts}/v1/webhooks/{sub_id}", bearer=token)
    return status


def build_verify_probe(rotated_at):
    """The synthetic delivery that proves the new ingest path accepts auth.

    A v2 CloudEvent with no requestToken: flow_handle_ping stops at
    GuardEventToken and ends at NoOp, without calling TSANet.
    """
    return {"specversion": "1.0",
            "id": f"rotation-verify-{uuid.uuid4()}",
            "source": "urn:tsanet-zendesk:rotation-verify",
            "type": "org.tsanet.connect.collaboration-request.note.created",
            "time": rotated_at,
            "datacontenttype": "application/json",
            "data": {}}


def sweep(ts, tsanet_token, zd, read_token, since, until):
    """Each case TSANet created in [since, until], with its ticket ids.

    Returns None if either lookup fails. Only ids, tokens and times are kept:
    both responses carry case and ticket content, which never leaves here.
    """
    query = urllib.parse.urlencode({"createdAfter": since})
    status, cases = request("GET", f"{ts}/v2/collaboration-requests/list?{query}",
                            bearer=tsanet_token)
    if status != 200:
        print(f"warning: cannot list TSANet cases (HTTP {status})", file=sys.stderr)
        return None
    lo, hi = parse_utc(since), parse_utc(until)
    found = []
    for case in cases:
        token, created = case.get("token"), case.get("createdAt")
        if not token or not created:
            print(f"warning: case {case.get('id')} has no token or createdAt; "
                  "skipped", file=sys.stderr)
            continue
        if not lo <= parse_utc(created) <= hi:
            continue
        query = urllib.parse.urlencode({"external_id": token})
        status, tickets = request("GET", f"{zd}/api/v2/tickets.json?{query}",
                                  bearer=read_token)
        if status != 200:
            print(f"warning: cannot look up tickets for case {case.get('id')} "
                  f"(HTTP {status})", file=sys.stderr)
            return None
        found.append({"case": case.get("id"), "token": token,
                      "created": created,
                      "tickets": [t["id"] for t in tickets.get("tickets", [])]})
    return found


def report(found, since, until, path):
    """Print the sweep's result, write it to path, return the duplicates."""
    dups = [f for f in found if len(f["tickets"]) > 1]
    single = sum(1 for f in found if len(f["tickets"]) == 1)
    print(f"sweep {since} to {until}: {len(found)} case(s), {single} with one "
          f"ticket, {len(found) - single - len(dups)} with none yet, "
          f"{len(dups)} with more than one")
    for d in dups:
        print(f"  DUPLICATE: case {d['case']} (token {d['token']}) has tickets "
              + ", ".join(str(t) for t in d["tickets"]))
    with open(path, "w") as f:
        json.dump({"since": since, "until": until, "cases": len(found),
                   "duplicates": dups}, f, indent=2)
    print(f"findings -> {path}")
    return dups


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="read-only: show what would be rotated, change nothing")
    ap.add_argument("--subscription-id", type=int,
                    help="TSANet subscription id to rotate (needed when no "
                         "callbackUrl ends in the old ingest path)")
    ap.add_argument("--integration", default=INTEGRATION_DEFAULT)
    ap.add_argument("--old-ingest-path",
                    help="ingest path of the current webhook; use when "
                         "OLD_WEBHOOK_UUID was never recorded (legacy "
                         "install). The old webhook then CANNOT be deleted "
                         "and its credential stays valid as an orphan.")
    ap.add_argument("--out", default="zis-webhook-credentials.json",
                    help="file to write the new ingest credentials to (chmod 600)")
    ap.add_argument("--sweep", action="store_true",
                    help="only check for duplicate tickets between --since and "
                         "--until; needs ZENDESK_READ_TOKEN, rotates nothing")
    ap.add_argument("--since", help="with --sweep: window start, UTC "
                                    "YYYY-MM-DDTHH:MM:SSZ")
    ap.add_argument("--until", help="with --sweep: window end (default: now)")
    ap.add_argument("--sweep-wait", type=int, default=120,
                    help="seconds to wait after the swap before sweeping")
    ap.add_argument("--findings", default=FINDINGS_DEFAULT,
                    help="file to write the sweep's findings to")
    args = ap.parse_args()

    if args.sweep:
        if not args.since:
            die("--sweep needs --since")
        env = require_env("ZENDESK_SUBDOMAIN", "TSANET_HOST", "TSANET_TOKEN",
                          "ZENDESK_READ_TOKEN")
        until = args.until or utc_now()
        try:
            parse_utc(args.since), parse_utc(until)
        except ValueError:
            die("--since and --until take UTC times like 2026-01-01T00:00:00Z")
        found = sweep(f"https://{env['TSANET_HOST']}", env["TSANET_TOKEN"],
                      f"https://{env['ZENDESK_SUBDOMAIN']}.zendesk.com",
                      env["ZENDESK_READ_TOKEN"], args.since, until)
        if found is None:
            die("sweep failed (see the warning above)")
        if report(found, args.since, until, args.findings):
            sys.exit(3)
        return

    env = require_env("ZENDESK_SUBDOMAIN", "ZIS_TOKEN", "TSANET_HOST",
                      "TSANET_TOKEN")
    read_token = os.environ.get("ZENDESK_READ_TOKEN")
    old_uuid = os.environ.get("OLD_WEBHOOK_UUID")
    if not old_uuid and not args.old_ingest_path:
        die("set OLD_WEBHOOK_UUID, or pass --old-ingest-path for a legacy "
            "install that never recorded the uuid (see --help)")

    zd = f"https://{env['ZENDESK_SUBDOMAIN']}.zendesk.com"
    ts = f"https://{env['TSANET_HOST']}"
    zis_webhooks = f"{zd}/api/services/zis/inbound_webhooks/generic/{args.integration}"

    # ── 1. Resolve the old webhook's ingest path ────────────────────────────
    if old_uuid:
        status, old = request("GET", f"{zis_webhooks}/{old_uuid}",
                              bearer=env["ZIS_TOKEN"])
        if status != 200:
            die(f"cannot show old webhook {old_uuid} (HTTP {status}). "
                "Check OLD_WEBHOOK_UUID is the `uuid` field, not the `id` ULID.")
        old_path = old["path"]
        print(f"old webhook: uuid={old_uuid}")
    else:
        old_path = args.old_ingest_path
        print("old webhook: uuid UNKNOWN (legacy install). The old webhook "
              "will NOT be deleted; its credential remains valid as an "
              "orphan. Treat the new webhook's uuid as a must-keep record.")

    # ── 2. Find the TSANet subscription pointing at it ──────────────────────
    sub, version = find_subscription(ts, env["TSANET_TOKEN"],
                                     args.subscription_id, old_path)
    print(f"subscription: id={sub['id']} on {version} "
          f"eventTypes={sub.get('eventTypes')} active={sub.get('active')} "
          f"auth={sub.get('callbackAuthType')}")

    # These checks stop the run before anything changes, dry run included.
    if sub.get("active") is False:
        die(f"subscription {sub['id']} is inactive, and a v2 subscription can't "
            "be created inactive, so rotating would turn its deliveries back "
            "on. Reactivate or delete it first.")
    check_bundle(zd, args.integration, env["ZIS_TOKEN"])
    event_types = replacement_event_types(sub, version)
    print(f"replacement: on v2 eventTypes={event_types}")
    if read_token:
        # A rejected token should stop the run now, not after the swap.
        query = urllib.parse.urlencode(
            {"external_id": f"rotation-preflight-{uuid.uuid4()}"})
        status, _ = request("GET", f"{zd}/api/v2/tickets.json?{query}",
                            bearer=read_token)
        if status != 200:
            die(f"ZENDESK_READ_TOKEN was rejected (HTTP {status}). Fix it, or "
                "unset it to rotate without the duplicate sweep.")
    else:
        print("WARNING: ZENDESK_READ_TOKEN is not set, so the duplicate-ticket "
              "sweep will be skipped. The command to run it later is printed "
              "at the end.")

    if args.dry_run:
        tail = ("delete the old subscription and old webhook" if old_uuid
                else "delete the old subscription (old webhook stays: uuid unknown)")
        print("\n--dry-run: no changes made. Would: create new ZIS webhook, "
              f"verify it, register a v2 subscription in place of {sub['id']}, "
              f"then {tail}.")
        return

    # ── 3. Create the replacement ZIS webhook (old one stays live) ──────────
    status, new = request("POST", zis_webhooks, bearer=env["ZIS_TOKEN"],
                          body={"source_system": "tsanet",
                                "event_type": "collaboration_event"})
    if status not in (200, 201):
        die(f"create new webhook failed (HTTP {status}): {new}")
    new_url = zd + new["path"] if new["path"].startswith("/") else new["path"]

    # Persist credentials BEFORE any cutover so they can never be lost.
    out = {"uuid": new["uuid"], "path": new["path"],
           "username": new["username"], "password": new["password"],
           "rotated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(out, f, indent=2)
    print(f"new webhook:  uuid={new['uuid']}  (credentials -> {args.out}, chmod 600)")

    def rollback(reason):
        print(f"verification failed ({reason}); rolling back", file=sys.stderr)
        status, _ = request("DELETE", f"{zis_webhooks}/{new['uuid']}",
                            bearer=env["ZIS_TOKEN"])
        if status not in (200, 204):
            # Keep the file: it is the only record of the new webhook's uuid.
            die(f"rotation aborted, but deleting the new webhook returned HTTP "
                f"{status}. Delete uuid={new['uuid']} by hand; its credentials "
                f"are in {args.out}. The old webhook and subscription are untouched.")
        os.unlink(args.out)
        die("rotation aborted; the old webhook and subscription are untouched")

    # ── 4. Verify the new ingest path, before any subscription points at it ─
    # Doing this first keeps the time both subscriptions are live (step 5 to
    # step 6), when a new case can get two tickets, as short as possible.
    # The probe ends at NoOp in the Integration Log; that is the probe.
    status, _ = request("POST", new_url,
                        basic=f"{new['username']}:{new['password']}",
                        body=build_verify_probe(out["rotated_at"]))
    if status != 200:
        rollback(f"synthetic delivery to new ingest URL returned HTTP {status}")
    print("verify: new ingest path accepts authenticated deliveries (HTTP 200)")

    # ── 5. Register the replacement subscription on v2, same scope ─────────
    swap_started = utc_now(-SWEEP_PAD_BEFORE)
    body = {"callbackUrl": new_url,
            "callbackAuth": {"type": "BASIC",
                             "username": new["username"],
                             "password": new["password"]}}
    # An empty or missing filter means "no filter" in both versions, and an
    # empty caseDirections fails v2's minItems: 1, so only non-empty ones go.
    if event_types:
        body["eventTypes"] = event_types
    for key in ("caseDirections", "partnerCompanyIds"):
        if sub.get(key):
            body[key] = sub[key]
    status, new_sub = request("POST", f"{ts}/v2/webhooks",
                              bearer=env["TSANET_TOKEN"], body=body)
    if status not in (200, 201):
        rollback(f"TSANet subscription create returned HTTP {status}: {new_sub}"
                 + ("; with no response it may exist anyway: look for one on "
                    "the new URL in GET /v2/webhooks and delete it" if not status
                    else ""))
    print(f"new subscription: id={new_sub.get('id')} on v2")

    # ── 6. Retire the old halves (this is the actual revocation) ────────────
    failed = webhook_deleted = False
    status = delete_subscription(ts, env["TSANET_TOKEN"], sub["id"])
    sub_deleted = status in (200, 204)
    if not sub_deleted:
        failed = True
    print(f"old subscription {sub['id']} delete: HTTP {status}"
          + ("" if status in (200, 204) else "  <-- FOLLOW UP MANUALLY: "
             "duplicate tickets until this or the old webhook is deleted"))
    if old_uuid:
        status, _ = request("DELETE", f"{zis_webhooks}/{old_uuid}",
                            bearer=env["ZIS_TOKEN"])
        webhook_deleted = status in (200, 204)
        if not webhook_deleted:
            failed = True
        print(f"old webhook delete: HTTP {status}"
              + ("" if status in (200, 204) else "  <-- FOLLOW UP MANUALLY: old credential still live"))
    else:
        print("old webhook delete: SKIPPED (uuid unknown). The old credential "
              "is orphaned, not revoked; "
              + ("the old subscription still delivers to it until deleted."
                 if failed else "nothing routes to it, but treat it as live "
                 "if it may have been exposed."))

    # Either delete ends the duplicates: the old webhook gone, the old
    # subscription's deliveries fail. With neither, the window is still open.
    window_open = not sub_deleted and not webhook_deleted
    swap_ended = None if window_open else utc_now(SWEEP_PAD_AFTER)
    out["swap_window"] = {"since": swap_started, "until": swap_ended}
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(out, f, indent=2)

    # Printed before the sweep's wait, so an interrupted wait loses nothing.
    store = (f"Store the credentials from {args.out} in your secret manager, "
             f"record uuid={new['uuid']}, then delete the file.")
    if failed:
        print(f"error: the new pipe is live but a retire step failed (FOLLOW "
              f"UP MANUALLY above). {store}", file=sys.stderr)
    else:
        print(f"\nrotation complete. {store}")

    # ── 7. Sweep for duplicate tickets created in the window ────────────────
    until_arg = swap_ended or f"<when you delete subscription {sub['id']}>"
    command = (f"python3 scripts/rotate-inbound-webhook.py --sweep "
               f"--since {swap_started} --until {until_arg}")
    if window_open:
        print(f"WARNING: subscription {sub['id']} and the old webhook are both "
              "still live, so new cases keep getting two tickets until you "
              "delete one of them.")
    dups = None
    if not read_token:
        print("duplicate sweep: SKIPPED (ZENDESK_READ_TOKEN not set).")
    else:
        print(f"duplicate sweep: waiting {args.sweep_wait}s for ZIS to run the "
              "window's deliveries")
        time.sleep(args.sweep_wait)
        until = swap_ended or utc_now()
        found = sweep(ts, env["TSANET_TOKEN"], zd, read_token, swap_started, until)
        if found is not None:
            dups = report(found, swap_started, until, args.findings)
    print("\nRun the sweep again in 15 minutes (ZIS can start a run minutes "
          f"after a delivery arrives):\n  {command}")
    if failed:
        sys.exit(1)
    if dups:
        sys.exit(3)


if __name__ == "__main__":
    main()
