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
import datetime
import glob
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

def check_all_workflows_declare_permissions(root):
    """Every workflow states its token scope explicitly.

    A workflow with no `permissions:` block inherits the repository default,
    which is broad (#109 found exactly that on zis-readme-check.yml). This is
    the rule that applies to ALL workflows; check_workflow_permissions() below
    holds release.yml to the stricter `{}`-plus-job-opt-in shape, because that
    one has jobs with write scopes.

    Globs rather than naming files. The previous version of this rule read only
    release.yml, so a workflow added later shipped unchecked while the audit
    stayed green (#139) — the same failure as #123, where the page check
    inspected a hardcoded list. Workflow-wide rules belong here, in a loop over
    the directory; only release-specific rules should name a file.

    Accepts a declaration at either level: top-level covers every job, and
    job-level is explicit per job. What fails is declaring neither.
    """
    cat = "supply-chain"
    wf_dir = os.path.join(root, ".github/workflows")
    undeclared = []
    checked = 0

    for name in sorted(os.listdir(wf_dir)):
        if not name.endswith((".yml", ".yaml")):
            continue
        checked += 1
        wf = read(root, f".github/workflows/{name}")
        top = re.search(r"^permissions:", wf, re.M)
        job = re.search(r"^\s+permissions:", wf, re.M)
        if not top and not job:
            undeclared.append(name)

    if not checked:
        record("FAIL", "every workflow declares its token scope",
               "no workflow files found — the check could not run, which is not a pass", cat)
    elif undeclared:
        record("FAIL", "every workflow declares its token scope",
               "no permissions block, so the repository default applies: "
               + "; ".join(undeclared), cat)
    else:
        record("PASS", "every workflow declares its token scope",
               f"all {checked} workflow(s) declare permissions explicitly", cat)


def check_workflow_permissions(root):
    """release.yml specifically: workflow scope empty, jobs opt in.

    Deliberately reads one file — this is a rule about the release workflow,
    not about workflows in general. The general rule lives in
    check_all_workflows_declare_permissions() above, which globs. Add
    workflow-wide checks there, not here (#139).
    """
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


def check_workflow_expression_injection(root):
    """Actions expressions must not be interpolated into a run: script.

    GitHub substitutes ${{ }} into the script text BEFORE any shell parses it,
    so quoting at the call site is not protection. A value containing shell
    metacharacters escapes its quotes and runs as code with the job's
    permissions.

    That is not theoretical here: git allows " $ ; ( ) in tag names (only space,
    ~ ^ : ? * [ \\ and control characters are forbidden), and the publish job
    holds attestations: write, so injected code could sign provenance over a
    tampered artifact. #138.

    The fix is always the same shape: bind the value in `env:` and reference it
    as "$NAME" in the script, where the shell treats it as data.

    Globs the directory rather than naming files, so a workflow added later is
    covered without anyone remembering to widen this (#123, #139).
    """
    cat = "supply-chain"
    wf_dir = os.path.join(root, ".github/workflows")
    offenders = []

    for name in sorted(os.listdir(wf_dir)):
        if not name.endswith((".yml", ".yaml")):
            continue
        lines = read(root, f".github/workflows/{name}").splitlines()
        base = None          # indent of the `run:` key while inside its block
        for n, line in enumerate(lines, 1):
            stripped = line.strip()
            indent = len(line) - len(line.lstrip())

            if base is not None:
                # A block scalar ends at the first non-blank line indented no
                # further than the key that introduced it.
                if stripped and indent <= base:
                    base = None
                elif "${{" in line:
                    offenders.append(f"{name}:{n}: {stripped[:60]}")
                    continue

            m = re.match(r"(\s*)-?\s*run:\s*(.*)$", line)
            if m:
                rest = m.group(2).strip()
                if rest in ("|", ">", "|-", ">-", "|+", ">+"):
                    base = indent          # block scalar follows
                elif "${{" in rest:
                    offenders.append(f"{name}:{n}: {rest[:60]}")

    if offenders:
        record("FAIL", "no Actions expressions inside run: scripts",
               "interpolated into shell: " + "; ".join(offenders), cat)
    else:
        record("PASS", "no Actions expressions inside run: scripts",
               "every ${{ }} is bound in env:/with:/if:, none reaches a shell script", cat)


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
    # Discover pages rather than listing them. A hardcoded list silently stops
    # covering the app the moment someone adds an entry point: deploy.html
    # shipped in v1.0.49 loading the SDK from a mutable channel with no SRI,
    # and this check reported PASS because it was not on the list
    # (tsanetgit/Zendesk_App#123, regressing #94 via #122).
    pattern = os.path.join(root, "zaf-build", "assets", "**", "*.html")
    pages = sorted(
        os.path.relpath(p, root) for p in glob.glob(pattern, recursive=True)
    )
    # An empty glob passing vacuously is the same failure class as a stale list.
    if not pages:
        record("FAIL", "SDK integrity", "no HTML pages found under "
               "zaf-build/assets/ — the check has nothing to inspect", cat)
        return
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

# A deadline this close is no longer a note for the next review to consider;
# it is a release-blocking defect, because the next release may well be the
# last one before the endpoint stops answering. 90 days is long enough to
# schedule and land a migration and short enough that no release crosses the
# line unremarked. Below this, the check FAILs and the release gate reports
# the build as uncovered (release.yml treats a nonzero audit as a coverage
# failure) rather than merely warning.
SUNSET_FAIL_WITHIN_DAYS = 90

# Files the endpoint scan reads. `.html` is here because zaf-build ships three
# HTML assets carrying inline script, and background.html alone holds three of
# the connector's four sunset call sites — omitting it is what let
# tsanetgit/Zendesk_App#144 read as a single-site problem for four releases
# (tsanetgit/Zendesk_App#145). An allowlist fails silently by construction: a
# future shipped asset type is invisible until someone remembers this line, so
# extend it whenever the bundle grows a new extension.
SCANNED_SUFFIXES = (".js", ".json", ".py", ".html")

# How a call to a sunsetting endpoint declares that it is deliberate:
#
#     tsanetGet('/webhooks')   // audit-allow: v1-webhooks
#
# Every use is a claim that the call survives its sunset, reviewable in the
# diff that adds it because it sits on the call rather than in a list
# somewhere else. Four conditions keep it from becoming a way to quiet the
# check, and each exists because the marker was found to over-clear without it:
#
#   1. It is read only on the line the call starts on.
#   2. It must NAME the entry it excuses, so a marker granted for one endpoint
#      does not cover a different sunsetting call sharing the line.
#   3. It must sit in a COMMENT and BEGIN AFTER the call it excuses. The token
#      inside a string literal, or in prose ahead of the call, is not a grant.
#   4. It excuses a line carrying exactly ONE matching call. Two calls and one
#      marker means the second was never granted anything, so the line flags;
#      put the second call on its own line and mark it too if it is deliberate.
#
# Condition 3's comment openers cover the scanned languages except JSON, which
# has no comment syntax and therefore cannot carry a marker. Nothing in a
# scanned .json needs one today; if that changes, this is the line to revisit.
#
# Markers that excused nothing are reported by check_deprecated_endpoints
# rather than ignored, so a typo'd key or one left behind by a call that moved
# surfaces instead of sitting there looking like protection.
ALLOW_MARKER = re.compile(r"(?://|#|/\*|<!--)[^\n]*?audit-allow:\s*([\w.-]+)")


def _sunset_urgency(dates, today=None):
    """Days until the nearest sunset in `dates`, or None if there are none.

    `today` is a parameter so the escalation can be exercised against a
    synthetic date instead of by waiting for the calendar.
    """
    today = today or datetime.date.today()
    remaining = [(datetime.date.fromisoformat(d) - today).days for d in dates]
    return min(remaining) if remaining else None


def _matches_unmarked(lines, pat, key, used=None):
    """True when `pat` hits a call site that no `audit-allow: <key>` excuses.

    Matching is per call site rather than per file, so a marker clears the call
    it sits on and nothing else. `used` collects the (line, column) of every
    marker that actually excused something, which is how the caller reports the
    ones that excused nothing.

    Two details make the per-line approach workable:

    - The search runs against a two-line window, because a call can wrap and
      put its version argument on the next physical line:

          tsanetGet('/webhooks',
                    'v2')

      Judged one line at a time, the lookahead cannot see the `'v2'` and a
      correctly migrated call reads as the deprecated form. The predictable
      response to that false flag is to add a marker, which would assert the
      call is a deliberate v1 one when it is not.

      One window is enough for how these calls are actually written, including
      a call opened on its own line, because the path and the version argument
      still land on adjacent lines. What still misreads is a version argument
      two or more lines below its path, which no signature here produces.
    - A match must START on the line being judged, so each call is considered
      once, and the marker is read from that line only. A marker on the
      following line belongs to whatever sits there.

    Every line is walked even after a hit, rather than returning early, so that
    markers further down the file are still credited as used.
    """
    hit = False
    for i, ln in enumerate(lines):
        window = ln if i + 1 >= len(lines) else ln + "\n" + lines[i + 1]
        starts_here = [m for m in re.finditer(pat, window) if m.start() < len(ln)]
        if not starts_here:
            continue
        # One marker excuses one call. With two matching calls on a line the
        # marker was granted for at most one of them, and which one is not
        # knowable, so the line flags and the author splits it.
        excuse = None
        if len(starts_here) == 1:
            for mk in ALLOW_MARKER.finditer(ln):
                if mk.group(1) == key and mk.start() >= starts_here[0].end():
                    excuse = mk
                    break
        if excuse is not None:
            if used is not None:
                used.add((i, excuse.start()))
            continue
        hit = True
    return hit


def check_deprecated_endpoints(root, today=None):
    cat = "platform-deadlines"
    # Endpoints with a published sunset that the connector still calls.
    #
    # Each pattern must match how the call is WRITTEN, not the URL it resolves
    # to, and it must stop matching once the call is migrated. Those two pull
    # in opposite directions here, because the ZAF app keeps the API version in
    # its base URL and writes paths relative to it: background.html:67 is
    # tsanetGet('/webhooks'), with no version in the literal at all.
    #
    # Matching the bare literal catches the call but can never be cleared — the
    # same string is correct against a v2 base — so the check would flag
    # forever and mean nothing, which is the defect this whole check was
    # rewritten to fix. Hence `needs_v1_base`: a relative path counts only in a
    # file that also declares the v1 base, so migrating the base clears it.
    # Absolute references carry their own version and need no context.
    # Two shapes, because tsanetgit/Zendesk_App#144 changed the second one.
    # Originally the version was baked into the host literal
    # ('https://connect2.tsanet.net/v1'); the migration moved it to a default
    # parameter (host + '/' + (version || 'v1')) so individual calls can opt
    # into v2. That edit alone made this check blind — the host literal no
    # longer contained /v1, so needs_v1_base gated off and a reintroduced
    # v1 call would not have been flagged. Demonstrated by injecting one and
    # watching the check pass, which is the only reason it was caught.
    v1_base = r"""connect2\.tsanet\.(?:net|org)/v1|\|\|\s*['"]v1['"]"""
    # (pattern, needs_v1_base, key, label, sunset, replacement)
    #
    # `key` is the name an audit-allow marker must use to excuse this entry.
    # The two webhook entries share one key on purpose: they are two spellings
    # of the same endpoint, so one marker covers the call however it is written.
    #
    # A deliberate v1 call declares itself on its own line. It is not inferred
    # from a v2 call elsewhere in the file: the probe below asks v1 AND v2 and
    # unions the results, and a file-scoped rule reading "this file has a v2
    # webhooks call, so its v1 webhooks calls are fine" would hide a NEW v1-only
    # call added to that file later.
    deprecated = [
        (r"/collaboration-requests\?", True, "v1-collaboration-list",
         "GET /v1/collaboration-requests (list)",
         "2027-01-01", "GET /v2/collaboration-requests"),
        # The lookahead is what makes a relative path readable as versioned:
        # tsanetGet('/webhooks') resolves to v1 through baseUrl's default and
        # counts, tsanetGet('/webhooks', 'v2') names its version and does not.
        # Without it the migrated call matches its own replacement pattern.
        #
        # The quote class includes the backtick because these are .js and .html
        # files, where a backtick is a template literal and tsanetGet(`/webhooks`)
        # is an ordinary call. Omitting it left that form invisible to the scan.
        (r"""['"`]/webhooks['"`](?!\s*,\s*['"`]v2['"`])""", True, "v1-webhooks",
         "v1 webhook registration/list", "2027-01-01", "/v2/webhooks"),
        # A URL expression joins the path to something without a space
        # (f"{ts}/v1/webhooks", "https://host/v1/webhooks"); prose puts a space
        # in front of it ("GET /v1/webhooks returned ..."). That is a shape
        # rule, not a wording rule, which is why comments naming the endpoint
        # do not flag the file that calls it. Comments are NOT skipped
        # wholesale, because commented-out code is worth flagging and a
        # commented-out call keeps its call shape.
        #
        # A backtick is deliberately NOT excluded here. It was, to spare prose
        # written as `/v1/webhooks`, and that also excused fetch(`/v1/webhooks`)
        # — real code, silently unscanned. Backticked prose flagging is the
        # cheaper mistake of the two, and nothing in the tree writes it.
        (r"(?<!\s)/v1/webhooks", False, "v1-webhooks",
         "v1 webhook registration/list", "2027-01-01", "/v2/webhooks"),
    ]
    found = []
    hit_dates = []
    stale_markers = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "dist", "node_modules")]
        for fn in files:
            if not fn.endswith(SCANNED_SUFFIXES):
                continue
            rel = os.path.relpath(os.path.join(base, fn), root)
            # Files that DESCRIBE the deprecated endpoints rather than call
            # them. Without the audit-record exclusion this check can never
            # reach PASS again: the record permanently names the call sites a
            # migration removed, so fixing the finding would not clear it.
            if rel in (os.path.join("scripts", "security-audit.py"),
                       ".security-audit.json"):
                continue
            try:
                body = read(root, rel)
            except OSError:
                continue
            on_v1_base = re.search(v1_base, body) is not None
            lines = body.splitlines()
            used = set()
            for pat, needs_v1_base, key, label, date, repl in deprecated:
                if needs_v1_base and not on_v1_base:
                    continue
                if _matches_unmarked(lines, pat, key, used):
                    found.append(f"{rel}: {label} (sunset {date}, use {repl})")
                    hit_dates.append(date)
            # A marker that excused nothing is not harmless. It reads in review
            # as a considered exemption while protecting nothing, and it is what
            # a typo'd key or a call that moved leaves behind.
            for i, ln in enumerate(lines):
                for mk in ALLOW_MARKER.finditer(ln):
                    if (i, mk.start()) not in used:
                        stale_markers.append(
                            f"{rel}:{i + 1}: audit-allow: {mk.group(1)}")

    if stale_markers:
        record("WARN", "every audit-allow marker excuses a real call",
               "marker excused nothing — stale, misspelled key, or the call it "
               "named has moved: " + "; ".join(sorted(set(stale_markers))), cat)
    else:
        record("PASS", "every audit-allow marker excuses a real call",
               "no unused audit-allow markers", cat)

    if not found:
        record("PASS", "no calls to sunsetting endpoints",
               "no known-deprecated endpoint usage found", cat)
        return

    days = _sunset_urgency(hit_dates, today)
    detail = "; ".join(sorted(set(found)))
    if days is not None and days <= SUNSET_FAIL_WITHIN_DAYS:
        n = abs(days)
        unit = "day" if n == 1 else "days"
        when = f"{days} {unit} away" if days >= 0 else f"{n} {unit} PAST"
        record("FAIL", "no calls to sunsetting endpoints",
               f"nearest sunset is {when} (threshold {SUNSET_FAIL_WITHIN_DAYS}d): {detail}", cat)
    else:
        record("WARN", "no calls to sunsetting endpoints",
               f"nearest sunset in {days} days: {detail}", cat)


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
        check_all_workflows_declare_permissions(root)
        check_workflow_permissions(root)
        check_workflow_expression_injection(root)
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
