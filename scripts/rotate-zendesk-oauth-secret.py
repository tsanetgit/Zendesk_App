#!/usr/bin/env python3
"""Rotate the Zendesk OAuth client secret behind the ZIS `zendesk` connection.

The ZIS connection that lets flows create and update tickets is backed by a
confidential OAuth client on your own Zendesk instance. This script rotates
that client's secret and re-points the ZIS registration at the new value.

Two platform behaviors shape the design (both verified live, 2026-07-24):

- `PUT /api/v2/oauth/clients/{id}/generate_secret` returns the new secret in
  full (the Admin UI shows it truncated; the API path is the only reliable
  one), and it invalidates the old secret IMMEDIATELY. There is no overlap
  window, so this script performs the regenerate and the ZIS update
  back-to-back. In-flight access tokens are short-lived either way; worst
  case the connection re-mints with the new secret on its next renewal.
- NEVER delete the OAuth client to rotate it: deleting a ZIS OAuth client
  registration cascade-deletes the connections built on it, which destroys
  the integration. Rotation is regenerate + PATCH, nothing else.

Required environment:
  ZENDESK_SUBDOMAIN   your Zendesk subdomain (the X in X.zendesk.com)
  SETUP_TOKEN         admin OAuth bearer for /api/v2 (Quick Start Step 1b)
  ZIS_TOKEN           ZIS OAuth bearer token (scopes: zis:read zis:write)

Usage:
  rotate-zendesk-oauth-secret.py [--dry-run] [--integration NAME]
                                 [--zis-client-name NAME]
                                 [--out FILE]

The new secret is written to --out (default: zendesk-oauth-secret.json,
chmod 600) BEFORE the ZIS update, so a failure between the two steps can
always be recovered by re-running the PATCH with the saved value. Store the
file contents in your secret manager, then delete the file.

If the ZIS update step fails, DO NOT re-run the whole script (that would
burn another secret). Re-run only the PATCH, as printed in the error output.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def request(method, url, bearer=None, body=None):
    req = urllib.request.Request(url, method=method)
    req.add_header("User-Agent", "tsanet-zendesk-rotate/1.0")
    if bearer:
        req.add_header("Authorization", f"Bearer {bearer}")
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
                    help="read-only: resolve and display both sides, change nothing")
    ap.add_argument("--integration", default="tsanet_connect")
    ap.add_argument("--zis-client-name", default="zendesk_self",
                    help="name of the ZIS OAuth client registration backing "
                         "the zendesk connection")
    ap.add_argument("--out", default="zendesk-oauth-secret.json",
                    help="file the new secret is written to (chmod 600)")
    args = ap.parse_args()

    env = {}
    for var in ("ZENDESK_SUBDOMAIN", "SETUP_TOKEN", "ZIS_TOKEN"):
        val = os.environ.get(var)
        if not val:
            die(f"missing required environment variable {var} (see --help)")
        env[var] = val
    zd = f"https://{env['ZENDESK_SUBDOMAIN']}.zendesk.com"

    # ── 1. Resolve the ZIS registration ─────────────────────────────────────
    status, listing = request("GET",
        f"{zd}/api/services/zis/connections/oauth/clients/{args.integration}",
        bearer=env["ZIS_TOKEN"])
    if status != 200:
        die(f"cannot list ZIS OAuth clients (HTTP {status})")
    regs = listing if isinstance(listing, list) else listing.get("clients", [])
    matches = [c for c in regs if c.get("name") == args.zis_client_name]
    if len(matches) != 1:
        die(f"expected exactly 1 ZIS registration named "
            f"{args.zis_client_name!r}, found {len(matches)} "
            f"(available: {[c.get('name') for c in regs]})")
    reg = matches[0]
    identifier = reg["client_id"]
    if reg.get("grant_type") != "client_credentials":
        die(f"registration {args.zis_client_name!r} is "
            f"{reg.get('grant_type')!r}, not client_credentials — this "
            "script only rotates the self-instance zendesk client")
    print(f"ZIS registration: name={reg['name']} uuid={reg['uuid']} "
          f"client_id={identifier} scopes={reg.get('default_scopes')!r}")

    # ── 2. Resolve the Zendesk OAuth client's numeric id ────────────────────
    status, cl = request("GET", f"{zd}/api/v2/oauth/clients.json",
                         bearer=env["SETUP_TOKEN"])
    if status != 200:
        die(f"cannot list Zendesk OAuth clients (HTTP {status}) — "
            "SETUP_TOKEN must be an admin bearer")
    targets = [c for c in cl.get("clients", []) if c.get("identifier") == identifier]
    if len(targets) != 1:
        die(f"expected exactly 1 Zendesk OAuth client with identifier "
            f"{identifier!r}, found {len(targets)}")
    numeric_id = targets[0]["id"]
    print(f"Zendesk OAuth client: identifier={identifier} id={numeric_id} "
          f"kind={targets[0].get('kind')}")

    if args.dry_run:
        print("\n--dry-run: no changes made. Would: regenerate the secret of "
              f"client {numeric_id}, then PATCH ZIS registration {reg['uuid']} "
              "with the new value.")
        return

    # ── 3. Regenerate (old secret dies NOW) and persist before ZIS update ───
    status, g = request("PUT",
        f"{zd}/api/v2/oauth/clients/{numeric_id}/generate_secret.json",
        bearer=env["SETUP_TOKEN"])
    new_secret = (g.get("client") or {}).get("secret")
    if status != 200 or not new_secret:
        die(f"generate_secret failed (HTTP {status}) — nothing was changed"
            if status != 200 else
            "generate_secret returned no secret — check the client manually "
            "before retrying; the old secret may already be invalid")
    out = {"client_identifier": identifier, "client_id": numeric_id,
           "zis_registration_uuid": reg["uuid"],
           "client_secret": new_secret,
           "rotated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(out, f, indent=2)
    print(f"secret regenerated (old secret is now invalid); "
          f"new value saved -> {args.out} (chmod 600)")

    # ── 4. PATCH the ZIS registration (PUT returns 405 — known gotcha) ──────
    status, _ = request("PATCH",
        f"{zd}/api/services/zis/connections/oauth/clients/{args.integration}/{reg['uuid']}",
        bearer=env["ZIS_TOKEN"], body={"client_secret": new_secret})
    if status not in (200, 204):
        print(f"\nCRITICAL: ZIS update failed (HTTP {status}). The Zendesk "
              "client already has the NEW secret; ZIS still holds the OLD "
              "one, so the connection will fail on its next token renewal.\n"
              "Do NOT re-run this script. Recover by re-running only the "
              "PATCH with the saved secret:\n"
              f"  curl -X PATCH '{zd}/api/services/zis/connections/oauth/"
              f"clients/{args.integration}/{reg['uuid']}' \\\n"
              "    -H 'Authorization: Bearer $ZIS_TOKEN' "
              "-H 'Content-Type: application/json' \\\n"
              f"    -d '{{\"client_secret\": \"<value from {args.out}>\"}}'",
              file=sys.stderr)
        sys.exit(2)
    print(f"ZIS registration {reg['uuid']} updated with the new secret")

    # ── 5. Verify the new secret mints tokens ───────────────────────────────
    status, _ = request("POST", f"{zd}/oauth/tokens",
                        body={"grant_type": "client_credentials",
                              "client_id": identifier,
                              "client_secret": new_secret,
                              "scope": "read"})
    print(f"verify: client_credentials mint with new secret -> HTTP {status} "
          + ("(OK)" if status == 200 else "<-- INVESTIGATE"))
    if status != 200:
        sys.exit(2)

    print("\nrotation complete. The connection re-mints its token with the "
          "new secret on its next renewal (tokens are short-lived, so within "
          f"the hour). Store the contents of {args.out} in your secret "
          "manager, then delete the file. If your SETUP_TOKEN workflow "
          "stores this client's secret anywhere else, update it there too.")


if __name__ == "__main__":
    main()
