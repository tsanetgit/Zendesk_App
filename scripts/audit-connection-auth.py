#!/usr/bin/env python3
"""Audit ZIS connection auth types ahead of the Zendesk API-token retirement.

Zendesk retires all API tokens on 2027-04-30 (no new tokens for new accounts
after 2026-07-28). A ZIS install whose Zendesk-side connection still uses a
token/basic credential loses inbound ticket creation on that date with no
warning. Documentation prescribing OAuth is not a control; this probe is.

Checks every connection registered under the integration and reports:
  PASS  the Zendesk-side connection uses OAuth, and no basic-auth or
        api-key connections exist
  FAIL  the Zendesk-side connection is token/basic-auth (2027-04-30
        breakage) or is missing entirely            -> exit 1
  WARN  Zendesk-side is fine, but other basic-auth / api-key / stale
        bearer-token connections exist (hygiene)    -> exit 2

Required environment:
  ZENDESK_SUBDOMAIN   your Zendesk subdomain (the X in X.zendesk.com)
  ZIS_TOKEN           ZIS OAuth bearer token (scopes: zis:read zis:write)

Usage:
  audit-connection-auth.py [--integration NAME] [--zendesk-connection NAME]
                           [--json]

Exit codes make it cron/CI-friendly: wire it into a scheduled job and alert
on non-zero.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

RETIREMENT_DATE = "2027-04-30"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--integration", default="tsanet_connect")
    ap.add_argument("--zendesk-connection", default="zendesk",
                    help="name of the ZIS connection used for Zendesk API "
                         "calls (the one hit by the token retirement)")
    ap.add_argument("--json", action="store_true",
                    help="emit a machine-readable report on stdout")
    args = ap.parse_args()

    subdomain = os.environ.get("ZENDESK_SUBDOMAIN")
    token = os.environ.get("ZIS_TOKEN")
    if not subdomain or not token:
        print("error: set ZENDESK_SUBDOMAIN and ZIS_TOKEN (see --help)",
              file=sys.stderr)
        sys.exit(3)

    url = (f"https://{subdomain}.zendesk.com/api/services/zis/integrations/"
           f"{args.integration}/connections/all")
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("User-Agent", "tsanet-zendesk-audit/1.0")
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"error: connections/all returned HTTP {e.code}", file=sys.stderr)
        sys.exit(3)

    # Buckets are keyed by auth type; values are lists of connections.
    # (A "meta" key may also be present; ignore non-list values.)
    buckets = {k: v for k, v in data.items() if isinstance(v, list)}
    inventory = [
        {"name": c.get("name"), "auth": bucket,
         "created_at": c.get("created_at"), "updated_at": c.get("updated_at")}
        for bucket, conns in buckets.items() for c in conns
    ]

    zd = [c for c in inventory if c["name"] == args.zendesk_connection]
    hygiene = [c for c in inventory
               if c["name"] != args.zendesk_connection
               and c["auth"] in ("basic_auth", "api_key", "bearer_token")]

    if not zd:
        status, exit_code = "FAIL", 1
        reason = (f"no connection named {args.zendesk_connection!r} found; "
                  "the Zendesk-side connection is missing or renamed "
                  "(pass --zendesk-connection)")
    elif zd[0]["auth"] != "oauth":
        status, exit_code = "FAIL", 1
        reason = (f"connection {args.zendesk_connection!r} uses "
                  f"{zd[0]['auth']} — it stops working when Zendesk retires "
                  f"API tokens on {RETIREMENT_DATE}. Migrate to an OAuth "
                  "client-credentials connection (QUICK_START.md Step 4a "
                  "+ zis/README.md prerequisites).")
    elif hygiene:
        status, exit_code = "WARN", 2
        reason = ("Zendesk-side connection is OAuth, but weaker-auth "
                  "connections remain: "
                  + ", ".join(f"{c['name']} ({c['auth']})" for c in hygiene)
                  + ". Delete them if retired, or migrate them.")
    else:
        status, exit_code = "PASS", 0
        reason = (f"connection {args.zendesk_connection!r} is OAuth; no "
                  "basic-auth, api-key, or bearer-token connections present")

    if args.json:
        print(json.dumps({"status": status, "reason": reason,
                          "retirement_date": RETIREMENT_DATE,
                          "integration": args.integration,
                          "connections": inventory}, indent=2))
    else:
        print(f"{status}: {reason}")
        for c in inventory:
            print(f"  {c['auth']:>13}  {c['name']}"
                  f"  (created {str(c['created_at'])[:10]},"
                  f" updated {str(c['updated_at'])[:10]})")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
