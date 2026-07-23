#!/usr/bin/env python3
"""Rotate the ZIS inbound-webhook Basic credential (make-before-break).

Replaces the TSANet -> ZIS ingest webhook and its Basic credential with a
fresh one, re-points the TSANet webhook subscription at it, verifies the new
path accepts an authenticated delivery, then deletes the old subscription and
old ZIS webhook. The old credential is fully revoked at the end: ZIS inbound
webhooks CAN be deleted (DELETE .../generic/{integration}/{uuid}, validated
2026-07-22) even though the Zendesk docs do not list the operation.

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

INTEGRATION_DEFAULT = "tsanet_connect"


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def request(method, url, bearer=None, basic=None, body=None):
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
        req.add_header("Content-Type", "application/json")
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


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="read-only: show what would be rotated, change nothing")
    ap.add_argument("--subscription-id", type=int,
                    help="TSANet subscription id to rotate (required if "
                         "callbackUrl matching is ambiguous)")
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
    status, subs = request("GET", f"{ts}/v1/webhooks", bearer=env["TSANET_TOKEN"])
    if status != 200:
        die(f"cannot list TSANet subscriptions (HTTP {status})")
    if args.subscription_id:
        matches = [s for s in subs if s["id"] == args.subscription_id]
    else:
        matches = [s for s in subs if s.get("callbackUrl", "").rstrip("/")
                   .endswith(old_path.rstrip("/"))]
    if len(matches) != 1:
        die(f"expected exactly 1 matching subscription, found {len(matches)}. "
            "Pass --subscription-id explicitly.")
    sub = matches[0]
    print(f"subscription: id={sub['id']} eventTypes={sub.get('eventTypes')} "
          f"auth={sub.get('callbackAuthType')}")

    if args.dry_run:
        tail = ("delete the old subscription and old webhook" if old_uuid
                else "delete the old subscription (old webhook stays: uuid unknown)")
        print("\n--dry-run: no changes made. Would: create new ZIS webhook, "
              f"re-point subscription {sub['id']}, verify, then {tail}.")
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
        request("DELETE", f"{zis_webhooks}/{new['uuid']}", bearer=env["ZIS_TOKEN"])
        os.unlink(args.out)
        die("rotation aborted; the old webhook and subscription are untouched")

    # ── 4. Create the replacement TSANet subscription ───────────────────────
    body = {"callbackUrl": new_url,
            "eventTypes": sub.get("eventTypes") or None,
            "callbackAuth": {"type": "BASIC",
                             "username": new["username"],
                             "password": new["password"]}}
    status, new_sub = request("POST", f"{ts}/v1/webhooks",
                              bearer=env["TSANET_TOKEN"], body=body)
    if status != 200:
        rollback(f"TSANet subscription create returned HTTP {status}: {new_sub}")
    print(f"new subscription: id={new_sub.get('id')}")

    # ── 5. Verify the new ingest path end to end ────────────────────────────
    # A synthetic note.created with an unknown token is a designed no-op in
    # flow_handle_ping (GuardCreate) but proves ingest auth + wiring. Expect
    # one failed flow run in the Integration Log; that is the probe, not a bug.
    status, _ = request("POST", new_url,
                        basic=f"{new['username']}:{new['password']}",
                        body={"eventType": "note.created",
                              "requestToken": "rotation-verify-synthetic",
                              "timestamp": out["rotated_at"]})
    if status != 200:
        request("DELETE", f"{ts}/v1/webhooks/{new_sub['id']}",
                bearer=env["TSANET_TOKEN"])
        rollback(f"synthetic delivery to new ingest URL returned HTTP {status}")
    print("verify: new ingest path accepts authenticated deliveries (HTTP 200)")

    # ── 6. Retire the old halves (this is the actual revocation) ────────────
    status, _ = request("DELETE", f"{ts}/v1/webhooks/{sub['id']}",
                        bearer=env["TSANET_TOKEN"])
    print(f"old subscription {sub['id']} delete: HTTP {status}"
          + ("" if status in (200, 204) else "  <-- FOLLOW UP MANUALLY"))
    if old_uuid:
        status, _ = request("DELETE", f"{zis_webhooks}/{old_uuid}",
                            bearer=env["ZIS_TOKEN"])
        print(f"old webhook delete: HTTP {status}"
              + ("" if status in (200, 204) else "  <-- FOLLOW UP MANUALLY: old credential still live"))
    else:
        print("old webhook delete: SKIPPED (uuid unknown). The old credential "
              "is orphaned, not revoked; nothing routes to it, but treat it "
              "as live if it may have been exposed.")

    print("\nrotation complete. Store the credentials from "
          f"{args.out} in your secret manager, record uuid={new['uuid']}, "
          "then delete the file.")


if __name__ == "__main__":
    main()
