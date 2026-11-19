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

Usage:
  rotate-inbound-webhook.py [--dry-run] [--subscription-id N]
                            [--integration NAME] [--out FILE]

New credentials are written to --out (default: zis-webhook-credentials.json,
chmod 600). They are never printed. Store the file contents in your secret
manager, then delete the file. Record the new webhook's uuid: ZIS has no
list operation, so a lost uuid cannot be recovered or deleted later.
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

INTEGRATION_DEFAULT = "tsanet_connect"
V1_SUNSET = "2027-01-01"   # GET and POST /v1/webhooks stop working

# v1 event type -> its v2 name (Connect-API-Code 3.2.5, WebhookEventType and
# WebhookEventTypeV2). v2's other three types have no v1 counterpart.
V1_TO_V2_EVENT_TYPES = {
    "collaboration-request.created":
        "org.tsanet.connect.collaboration-request.created",
    "note.created": "org.tsanet.connect.collaboration-request.note.created",
}


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
    args = ap.parse_args()

    env = {}
    for var in ("ZENDESK_SUBDOMAIN", "ZIS_TOKEN", "TSANET_HOST", "TSANET_TOKEN"):
        val = os.environ.get(var)
        if not val:
            die(f"missing required environment variable {var} (see --help)")
        env[var] = val
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
    failed = False
    status = delete_subscription(ts, env["TSANET_TOKEN"], sub["id"])
    if status not in (200, 204):
        failed = True
    print(f"old subscription {sub['id']} delete: HTTP {status}"
          + ("" if status in (200, 204) else "  <-- FOLLOW UP MANUALLY: "
             "duplicate tickets until this or the old webhook is deleted"))
    if old_uuid:
        status, _ = request("DELETE", f"{zis_webhooks}/{old_uuid}",
                            bearer=env["ZIS_TOKEN"])
        if status not in (200, 204):
            failed = True
        print(f"old webhook delete: HTTP {status}"
              + ("" if status in (200, 204) else "  <-- FOLLOW UP MANUALLY: old credential still live"))
    else:
        print("old webhook delete: SKIPPED (uuid unknown). The old credential "
              "is orphaned, not revoked; "
              + ("the old subscription still delivers to it until deleted."
                 if failed else "nothing routes to it, but treat it as live "
                 "if it may have been exposed."))

    store = (f"Store the credentials from {args.out} in your secret manager, "
             f"record uuid={new['uuid']}, then delete the file.")
    if failed:
        die(f"the new pipe is live but a retire step failed (FOLLOW UP "
            f"MANUALLY above). {store}")
    print(f"\nrotation complete. {store}")


if __name__ == "__main__":
    main()
