#!/usr/bin/env python3
"""Mechanical security checks for the TSANet Zendesk connector.

The deterministic floor of the connector security review: checks that can
be decided from the repository contents (plus, optionally, one live CDN
fetch) with no human judgement. Everything requiring judgement lives in the
review taxonomy and is performed by a reviewer, not here.

Check set derived from the EDB Security Design Review (2026-07-08), issues
tsanetgit/Zendesk_App#90 to #98.

Exit codes (cron/CI friendly):
  0  all checks passed
  1  one or more FAIL results
  2  no FAILs, one or more WARN results
  3  the audit could not run (bad repo root, unreadable files)

Usage:
  security-audit.py [--json] [--no-network] [--repo-root PATH]

--no-network skips the checks that fetch the pinned SDK from the CDN; those
are reported as SKIP rather than counted as passes.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request

RESULTS = []


def record(status, check, detail, category):
    RESULTS.append({"status": status, "check": check,
                    "detail": detail, "category": category})


def read(root, rel):
    path = os.path.join(root, rel)
    with open(path, encoding="utf-8") as f:
        return f.read()


# ── Category 1: supply chain and release integrity ──────────────────────

def check_workflow_permissions(root):
    cat = "supply-chain"
    try:
        wf = read(root, ".github/workflows/release.yml")
    except OSError as e:
        record("FAIL", "release workflow present", str(e), cat)
        return
    # Workflow-scope permissions must be empty; jobs opt in.
    top = re.search(r"^permissions:\s*(\{\}|\n)", wf, re.M)
    if re.search(r"^permissions:\s*\{\}\s*$", wf, re.M):
        record("PASS", "release token least-privilege",
               "workflow-scope permissions: {}", cat)
    elif top:
        record("FAIL", "release token least-privilege",
               "workflow-scope permissions grants scopes; jobs should opt in "
               "individually", cat)
    else:
        record("FAIL", "release token least-privilege",
               "no explicit permissions block; the default token is broad", cat)

    if re.search(r"^\s*environment:\s*\S+", wf, re.M):
        record("PASS", "release approval gate",
               "publish job runs in a protected environment", cat)
    else:
        record("FAIL", "release approval gate",
               "no environment gate on the publishing job", cat)

    if "attest-build-provenance" in wf:
        record("PASS", "artifact provenance", "attestation step present", cat)
    else:
        record("FAIL", "artifact provenance",
               "no build-provenance attestation in the release workflow", cat)

    if "sha256sum" in wf or "shasum" in wf:
        record("PASS", "artifact checksum", "checksum step present", cat)
    else:
        record("WARN", "artifact checksum",
               "no checksum published alongside the artifact", cat)


def check_action_pinning(root):
    cat = "supply-chain"
    wf_dir = os.path.join(root, ".github/workflows")
    unpinned = []
    for name in sorted(os.listdir(wf_dir)):
        if not name.endswith((".yml", ".yaml")):
            continue
        for m in re.finditer(r"uses:\s*([\w.-]+/[\w.-]+)@(\S+)", read(root, f".github/workflows/{name}")):
            action, ref = m.group(1), m.group(2)
            if not re.fullmatch(r"[0-9a-f]{40}", ref):
                unpinned.append(f"{name}: {action}@{ref}")
    if unpinned:
        record("WARN", "third-party actions SHA-pinned",
               "moving refs: " + "; ".join(unpinned), cat)
    else:
        record("PASS", "third-party actions SHA-pinned",
               "all actions pinned to a full commit SHA", cat)


def check_sdk_sri(root, network):
    cat = "supply-chain"
    pages = ["zaf-build/assets/index.html", "zaf-build/assets/background.html"]
    tags = {}
    for page in pages:
        try:
            html = read(root, page)
        except OSError as e:
            record("FAIL", f"SDK integrity ({page})", str(e), cat)
            continue
        m = re.search(r'<script[^>]*zaf_sdk\.min\.js[^>]*>', html, re.S)
        if not m:
            record("WARN", f"SDK integrity ({page})",
                   "no ZAF SDK script tag found", cat)
            continue
        tag = m.group(0)
        src = re.search(r'src="([^"]+)"', tag)
        integrity = re.search(r'integrity="(sha\d{3}-[A-Za-z0-9+/=]+)"', tag)
        if not integrity:
            record("FAIL", f"SDK integrity ({page})",
                   "script tag has no integrity attribute", cat)
            continue
        if "crossorigin" not in tag:
            record("FAIL", f"SDK integrity ({page})",
                   "integrity present but crossorigin missing; SRI will not "
                   "be enforced", cat)
            continue
        url = src.group(1) if src else ""
        # A channel URL (…/2.0/ or …/2/) is mutable: SRI against it breaks on
        # the vendor's next silent patch. Require an exact version.
        if not re.search(r"/\d+\.\d+\.\d+/", url):
            record("FAIL", f"SDK integrity ({page})",
                   f"SRI pinned against a mutable channel URL ({url}); this "
                   "breaks on the vendor's next patch. Pin an exact version.",
                   cat)
            continue
        record("PASS", f"SDK integrity ({page})",
               "exact-version URL with integrity + crossorigin", cat)
        tags[page] = (url, integrity.group(1))

    if len(tags) > 1 and len(set(tags.values())) > 1:
        record("WARN", "SDK integrity consistency",
               "pages load different SDK versions or hashes", cat)

    if not tags:
        return
    if not network:
        record("SKIP", "SDK hash matches CDN", "--no-network", cat)
        return
    url, declared = next(iter(tags.values()))
    algo = declared.split("-", 1)[0]
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "tsanet-security-audit/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read()
    except (urllib.error.URLError, OSError) as e:
        record("WARN", "SDK hash matches CDN", f"could not fetch {url}: {e}", cat)
        return
    digest = base64.b64encode(
        hashlib.new({"sha256": "sha256", "sha384": "sha384",
                     "sha512": "sha512"}[algo], body).digest()).decode()
    if f"{algo}-{digest}" == declared:
        record("PASS", "SDK hash matches CDN",
               "declared integrity matches the pinned artifact", cat)
    else:
        record("FAIL", "SDK hash matches CDN",
               "declared integrity does NOT match the artifact currently "
               "served at the pinned URL; the app will refuse to load the SDK",
               cat)


# ── Category 2/3: credentials and authorization ─────────────────────────

def check_no_embedded_secrets(root):
    cat = "credentials"
    # Long base64/hex runs and obvious credential assignments in shipped files.
    patterns = [
        (re.compile(r"(?i)(client_secret|api[_-]?token|password)\s*[:=]\s*[\"'][^\"'{}$][^\"']{12,}"), "credential-shaped assignment"),
        (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "Slack token"),
        (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "GitHub PAT"),
    ]
    hits = []
    for base, _, files in os.walk(os.path.join(root, "zaf-build")):
        for fn in files:
            if not fn.endswith((".js", ".json", ".html")):
                continue
            rel = os.path.relpath(os.path.join(base, fn), root)
            try:
                body = read(root, rel)
            except OSError:
                continue
            for pat, label in patterns:
                for m in pat.finditer(body):
                    # {{setting.x}} placeholders are the correct pattern
                    if "{{" in m.group(0) or "settings." in m.group(0):
                        continue
                    hits.append(f"{rel}: {label}")
    if hits:
        record("FAIL", "no embedded credentials in shipped bundle",
               "; ".join(sorted(set(hits))), cat)
    else:
        record("PASS", "no embedded credentials in shipped bundle",
               "no credential-shaped literals in zaf-build/", cat)


def check_secure_settings(root):
    cat = "credentials"
    try:
        manifest = json.loads(read(root, "zaf-build/manifest.json"))
    except (OSError, ValueError) as e:
        record("FAIL", "manifest readable", str(e), cat)
        return
    params = {p["name"]: p for p in manifest.get("parameters", [])}
    # field_id_* are Zendesk custom field identifiers, not credentials, even
    # though some are named "...token". Matching on the substring alone
    # produces a false positive, which costs more trust than it saves.
    secretish = [n for n in params
                 if re.search(r"(?i)password|secret|token|key", n)
                 and not n.startswith("field_id_")]
    bad = [n for n in secretish if not params[n].get("secure")]
    if bad:
        record("FAIL", "credential settings marked secure",
               f"not secure:true -> {', '.join(bad)}", cat)
    elif secretish:
        record("PASS", "credential settings marked secure",
               f"{len(secretish)} credential setting(s) all secure:true", cat)
    else:
        record("WARN", "credential settings marked secure",
               "no credential-shaped settings found to check", cat)

    wl = manifest.get("domainWhitelist")
    if wl:
        record("PASS", "egress constrained",
               f"domainWhitelist limits egress to {len(wl)} host(s)", cat)
    else:
        record("FAIL", "egress constrained",
               "no domainWhitelist; the app may call arbitrary hosts", cat)


# ── Category 6: external deadlines ──────────────────────────────────────

def check_deprecated_endpoints(root):
    cat = "platform-deadlines"
    # Endpoints with a published sunset that the connector still calls.
    deprecated = {
        r"/collaboration-requests\?": ("GET /v1/collaboration-requests (list)",
                                       "2027-01-01", "GET /v2/collaboration-requests"),
        r"/v1/webhooks": ("v1 webhook registration/list", "2027-01-01",
                          "/v2/webhooks"),
    }
    found = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "dist", "node_modules")]
        for fn in files:
            if not fn.endswith((".js", ".json", ".py")):
                continue
            rel = os.path.relpath(os.path.join(base, fn), root)
            if rel == os.path.join("scripts", "security-audit.py"):
                continue  # this file names the patterns it looks for
            try:
                body = read(root, rel)
            except OSError:
                continue
            for pat, (label, date, repl) in deprecated.items():
                if re.search(pat, body):
                    found.append(f"{rel}: {label} (sunset {date}, use {repl})")
    if found:
        record("WARN", "no calls to sunsetting endpoints",
               "; ".join(sorted(set(found))), cat)
    else:
        record("PASS", "no calls to sunsetting endpoints",
               "no known-deprecated endpoint usage found", cat)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-network", action="store_true")
    ap.add_argument("--repo-root", default=os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))
    args = ap.parse_args()
    root = args.repo_root

    if not os.path.isdir(os.path.join(root, "zaf-build")):
        print(f"error: {root} does not look like the connector repo", file=sys.stderr)
        sys.exit(3)

    try:
        check_workflow_permissions(root)
        check_action_pinning(root)
        check_sdk_sri(root, network=not args.no_network)
        check_no_embedded_secrets(root)
        check_secure_settings(root)
        check_deprecated_endpoints(root)
    except Exception as e:  # noqa: BLE001 - an audit that crashes must not read as a pass
        print(f"error: audit aborted: {e}", file=sys.stderr)
        sys.exit(3)

    fails = [r for r in RESULTS if r["status"] == "FAIL"]
    warns = [r for r in RESULTS if r["status"] == "WARN"]

    if args.json:
        print(json.dumps({"results": RESULTS,
                          "summary": {"pass": sum(1 for r in RESULTS if r["status"] == "PASS"),
                                      "fail": len(fails), "warn": len(warns),
                                      "skip": sum(1 for r in RESULTS if r["status"] == "SKIP")}},
                         indent=2))
    else:
        icon = {"PASS": "PASS", "FAIL": "FAIL", "WARN": "WARN", "SKIP": "SKIP"}
        for r in RESULTS:
            print(f"{icon[r['status']]:5s} [{r['category']}] {r['check']}")
            if r["status"] != "PASS":
                print(f"        {r['detail']}")
        print(f"\n{len(fails)} fail, {len(warns)} warn, "
              f"{sum(1 for r in RESULTS if r['status'] == 'PASS')} pass")
        print("\nMechanical checks are the floor, not the review. The design "
              "review (credential lifecycle, data retention, authorization "
              "boundaries, audit trail) is performed by a reviewer against "
              "the connector security taxonomy.")

    sys.exit(1 if fails else (2 if warns else 0))


if __name__ == "__main__":
    main()
