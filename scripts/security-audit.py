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

Tree-wide checks read the files git reports for PATH (tracked, plus untracked
files that are not ignored), so a second checkout living inside the tree — a
Claude Code worktree under .claude/worktrees/, say — is not scanned as though
it were this one. A PATH that is not a git repository falls back to a pruned
filesystem walk, so an exported tree still audits.
"""
import argparse
import base64
import collections
import datetime
import glob
import hashlib
import json
import os
import re
import subprocess
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


# This file's own path inside the repo. Spelled once because it is used twice
# for two different jobs — as the sentinel the enumeration is checked against,
# and as a file the deprecation scan skips reading — and two spellings of one
# path are a drift waiting to happen.
AUDIT_SCRIPT = os.path.join("scripts", "security-audit.py")

# Named fields, so a swapped unpacking cannot quietly misreport the source.
Scan = collections.namedtuple("Scan", "paths source degraded")


def _tree_files(root):
    """The files belonging to THIS repository tree, and where the list came from.

    Tree-wide checks enumerate here. Checks anchored under zaf-build/ keep
    their own glob or walk: no second checkout lands inside zaf-build/, and
    routing them through this would change what they scan for no gain.

    `git ls-files` rather than a raw filesystem walk, because a walk cannot
    tell this tree's files from a SECOND CHECKOUT sitting inside it. Claude
    Code creates linked worktrees under .claude/worktrees/<name>/, each at
    whatever commit that session was on, and the walk read them as ordinary
    directories. So the audit scanned two copies of the repo at two different
    revisions: measured on main at 7907fe1, a clone with one nested worktree
    reported 3 warn / 17 pass where a clean export of the same commit reported
    2 warn / 18 pass. The extra WARN was `every audit-allow marker excuses a
    real call`, firing on markers that live in the worktree's copy of THIS
    file, and the two real WARNs listed every finding twice, once per copy.
    The per-path skip in check_deprecated_endpoints could not stop it: the
    copies enumerate as `.claude/worktrees/<name>/scripts/security-audit.py`,
    which is not the string it compares against.

    `--cached` ONLY. The audit measures the commit, which is what CI builds
    from and what members install; the working tree it happens to be run
    beside is not the subject. An earlier version added
    `--others --exclude-standard` to keep untracked-but-not-ignored files in
    scope, on the reasoning that the local value of the audit is the run
    before `git add`. That convenience cost the property, and the property is
    what tsanetgit/Zendesk_App#185 asks for:

      - one untracked file at the repo root carrying a stale marker moved a
        clean run from 19 pass / 2 warn to 18 pass / 3 warn, which is #185's
        third acceptance criterion failing verbatim;
      - an untracked PARTIAL copy of the bundle (vendor_copy/zaf-build/assets/
        background.html, no .git of its own) got background.html reported
        TWICE while check_scanned_tree still said PASS. That is the exact
        double-reporting the guard below exists to prevent, reaching it
        through a shape none of the guard's three tests can see: no duplicate
        sentinel, because the copy carries no scripts/security-audit.py; no
        nested .git; no directory entry.

    So `--others` reopened a narrower form of the class #184 had just closed,
    and no check could catch it, because every path involved is a well-formed
    path to a real file. Removing the flag closes it by construction rather
    than by adding a fourth test to the guard.

    Staged work is still covered: `--cached` reads the INDEX, so `git add`
    followed by the audit sees a new file. What is no longer covered is the
    run before `git add`, which is the deliberate trade — and it is a smaller
    trade than it sounds, because `git add -N` is enough. That records the
    path in the index without staging its content, so a brand-new file is back
    in scope while still showing as unstaged. Probed both ways: untracked
    alone is not flagged, and the same file after `git add -N` is.

    Nothing else needs an exclusion. Tracked paths cannot be ignored, so the
    dist/ and node_modules/ prune this list once did by hand is inherent, and
    .claude/worktrees/ is untracked and therefore out of scope by the same
    rule rather than by .git/info/exclude honouring an ignore file.

    Two routes reach the pruned walk instead, and only one of them is a
    problem. A `root` that is not a checkout is ordinary — an exported tree
    (`git archive | tar -x`) is how this bug was diagnosed, so it stays
    runnable — and reports PASS. A `root` that IS a checkout whose git did not
    answer is degraded, and says so, because there git was the right tool.

    Scope the tracked-only property honestly, in two directions.

    It holds on the git route, and on an export, where every file present IS
    the commit. It does NOT hold in degraded mode, because a walk with no git
    cannot tell tracked from untracked, so an untracked file at the root is
    back in scope there. That is inherent rather than an oversight, and it is
    the reason the degraded route WARNs instead of passing quietly.

    And this selects WHICH files are read, not WHAT is read from them: the
    list comes from the index, the bytes come from the working tree. They
    coincide on CI and in a fresh clone, and diverge locally in BOTH
    directions, so "the audit measures the commit" above is the intent rather
    than a literal guarantee:

      - a tracked file with unstaged edits is scanned with its EDITED content,
        which is usually what a person running this wants and is not what a
        clean export would produce. A false positive, and a loud one.
      - a path in the index whose file is absent from disk (`git add` then
        `rm`) is enumerated, fails to open, and is skipped by the `except
        OSError` in the scan loop. Its content IS in what would be committed
        and the audit reports clean. A false NEGATIVE, and a silent one.

    The second is pre-existing rather than introduced here — `read()` has
    always read from disk, and `--others` never helped for a file that is not
    on disk — and it is named because this paragraph is where a reader looks
    for the limit. Closing it means reading index blobs (`git cat-file -p
    :<path>`), which is a much larger change than #185 asked for.
    """
    # Gate on root being a checkout in its own right, and do not merely let a
    # failed git call fall through. git SUCCEEDS for an ANCESTOR repository: an
    # export unpacked inside some other checkout — /tmp is not always outside
    # one — enumerates through the ANCESTOR's ignore rules, silently. Probed by
    # putting a clean export of this repo in a scratch repo whose .gitignore
    # said `*.json`: the audit read 39 files instead of 44, every .json in the
    # tree (including zaf-build/manifest.json) dropped out of scope, and
    # check_scanned_tree PASSED, because the sentinel is a .py, nothing was a
    # nested checkout and nothing was a directory. That is this bug one
    # directory level up, so it is closed by construction rather than watched.
    # A linked worktree keeps its `.git` as a FILE, so exists() is the test,
    # not isdir().
    if not os.path.exists(os.path.join(root, ".git")):
        return Scan(_walk_files(root),
                    "filesystem walk (not a git repository)", False)
    try:
        # Do NOT add --others here. See the docstring: it puts untracked files
        # in scope, and an untracked copy of a bundle file is then reported as
        # a second finding under a PASSing guard (#185).
        out = subprocess.run(
            ["git", "-C", root, "ls-files", "-z", "--cached"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        # Reached only when root IS a checkout, so git was the right tool and
        # did not answer: no git on PATH, a timeout, a nonzero exit. Degraded
        # rather than expected, hence the flag.
        #
        # Carry git's own first line into the message. Discarding stderr left
        # the WARN saying only THAT the enumeration degraded, never why, which
        # is the difference between a reader fixing it and a reader shrugging.
        # CalledProcessError and TimeoutExpired carry .stderr; OSError (no git
        # on PATH) has none, and its str() is the useful text there.
        #
        # decode(), NOT os.fsdecode(): stderr is a message, not a path. fsdecode
        # applies surrogateescape, and a lone surrogate raises UnicodeEncodeError
        # the moment main() prints the report — an audit crashing inside the
        # branch added to make its failures legible. Probed: os.fsdecode(b"\xff")
        # crashes on print to a UTF-8 stream, "replace" prints as U+FFFD.
        err = getattr(e, "stderr", None) or b""
        lines = err.decode("utf-8", "replace").strip().splitlines()
        why = (lines[0] if lines else str(e))[:120]
        return Scan(_walk_files(root),
                    f"filesystem walk (git could not enumerate this tree: "
                    f"{why})", True)
    # Split on the NUL bytes before decoding, because a tracked path is not
    # required to be valid UTF-8. git separates with `/` on every platform;
    # normalising to os.sep keeps these paths interchangeable with the
    # os.path.relpath ones _walk_files produces, which the sentinel comparison
    # and the `zaf-build` prefix test below both assume. A no-op on POSIX.
    #
    # A SET, because --cached emits an unmerged path once per stage: during a
    # conflicted merge this command returns the same path three times. Left as
    # a list that made check_scanned_tree report "3 copies of
    # scripts/security-audit.py ... a second checkout is being scanned",
    # naming a checkout that does not exist, and inflated the deliberate-call
    # counts, which are len() of a list whose detail line renders through
    # sorted(set(...)) — a WARN that says 3 and then lists 1. Deduplicating
    # HERE rather than guarding downstream makes both unreachable by
    # construction, and it cannot weaken the duplicate-sentinel check, whose
    # inputs are DISTINCT paths (`.claude/worktrees/<x>/scripts/…` versus
    # `scripts/…`) that a set preserves. Probed both ways.
    return Scan(sorted({os.fsdecode(p).replace("/", os.sep)
                        for p in out.stdout.split(b"\0") if p}),
                "git ls-files", False)


def _walk_files(root):
    """Fallback enumeration: files under `root` that are not in another tree.

    Prunes four names outright — .git, .claude, dist and node_modules, the
    last two carried over from the prune this replaced — plus any directory
    holding a `.git` entry, which is what a nested checkout looks like from
    outside. That last rule tests for an ENTRY and not a directory on purpose:
    a linked git worktree carries a `.git` FILE (93 bytes in the clone this
    was found in), so a directory test would walk straight into it.
    """
    out = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs
                   if d not in (".git", ".claude", "dist", "node_modules")
                   and not os.path.exists(os.path.join(base, d, ".git"))]
        out.extend(os.path.relpath(os.path.join(base, fn), root)
                   for fn in files)
    return sorted(out)


def check_scanned_tree(root, scan):
    """The tree-wide scan covers this tree, all of it, and nothing else.

    A guard on the enumeration itself, because what it catches is invisible in
    the findings: a second checkout inside the repo does not make the audit
    error, it makes the audit report another revision's code as this one's,
    with every real finding listed twice. Nothing downstream can notice that —
    the duplicate is a well-formed path to a real file with real content.
    """
    cat = "supply-chain"
    name = "the scanned tree is exactly this tree"
    problems = []

    # Suffix-aware, NOT equality. The contaminating copy enumerates as
    # `.claude/worktrees/<name>/scripts/security-audit.py`, so counting exact
    # matches returns 1 on the contaminated tree exactly as on the clean one,
    # and the check would pass on the single input it exists to reject. That is
    # the same prefix blindness that let check_deprecated_endpoints' per-path
    # skip miss those copies in the first place.
    sentinels = [p for p in scan.paths
                 if p == AUDIT_SCRIPT or p.endswith(os.sep + AUDIT_SCRIPT)]
    if not sentinels:
        problems.append(
            f"the enumeration does not contain {AUDIT_SCRIPT}, so it is not "
            f"listing this repository at all; the checks reading it would pass "
            f"vacuously")
    elif len(sentinels) > 1:
        problems.append(
            f"{len(sentinels)} copies of {AUDIT_SCRIPT} are in scope, so a "
            f"second checkout is being scanned as though it were this tree: "
            + "; ".join(sentinels))

    # The sentinel catches a second copy of THIS repo. A nested checkout of any
    # OTHER repo has no sentinel to duplicate, so assert the boundary directly.
    nested = set()
    for rel in scan.paths:
        parts = rel.split(os.sep)[:-1]
        for i in range(1, len(parts) + 1):
            d = os.sep.join(parts[:i])
            if os.path.exists(os.path.join(root, d, ".git")):
                nested.add(d)
    if nested:
        problems.append("scanned paths sit inside a nested checkout: "
                        + "; ".join(sorted(nested)))

    # A submodule enumerates as one gitlink entry and an untracked nested
    # repository as a bare directory; either way the contents are never read.
    # Neither exists in this tree today, and both should arrive loudly rather
    # than quietly halving what the audit sees.
    dirs = sorted(p for p in scan.paths
                  if os.path.isdir(os.path.join(root, p)))
    if dirs:
        problems.append("enumerated as directories, so their contents go "
                        "unscanned: " + "; ".join(dirs))

    if problems:
        record("FAIL", name, " | ".join(problems), cat)
    elif scan.degraded:
        record("WARN", name,
               f"{len(scan.paths)} file(s) via {scan.source}; this root IS a "
               f"git repository, so the walk is a degraded substitute for its "
               f"index", cat)
    else:
        record("PASS", name, f"{len(scan.paths)} file(s) via {scan.source}", cat)


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
#     tsanetGet('/webhooks')   // audit-allow: v1-webhooks until 2027-01-01
#
# Every use is a claim that the call survives its sunset, reviewable in the
# diff that adds it because it sits on the call rather than in a list
# somewhere else. Six conditions keep it from becoming a way to quiet the
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
#   5. It must carry an EXPIRY (`until <YYYY-MM-DD>`). An undated marker excuses
#      nothing, and is reported rather than ignored so the author learns why.
#      Past its date it stops excusing and the check FAILs (#151).
#   6. It stops excusing entirely once the entry's sunset is within
#      SUNSET_FAIL_WITHIN_DAYS, whatever its expiry says, so the scheduled
#      escalation cannot be deferred by a marker (#151).
#
# Even while a marker is valid the excused call is still REPORTED, by a
# separate check, so the suite never returns a silent all-clear while a
# sunsetting call is in the shipped bundle. That was #151: a marked call left
# both `found` and `hit_dates`, so migrating the one unmarked call site
# elsewhere in the tree produced PASS at exit 0 with a live v1 call still in
# the bundle members install, and the dated escalation never fired because it
# is driven by an empty `hit_dates`.
#
# Condition 3's comment openers cover the scanned languages except JSON, which
# has no comment syntax and therefore cannot carry a marker. Nothing in a
# scanned .json needs one today; if that changes, this is the line to revisit.
#
# `//` is qualified with (?<!:) so the one inside `https://` does not read as a
# comment opener. Without it, a deliberate ABSOLUTE v1 call could not be marked
# at all: the opener matched at the scheme, so the marker's start landed before
# the call rather than after it, and the line emitted two contradictory
# warnings — "this marker excused nothing" and "this call is unmarked" — with
# no edit that satisfied both.
#
# That is a heuristic, not a parse. Whether a position is inside a comment is
# not decidable by regex, and the same collision exists in principle for a `#`
# in a URL fragment or a `/*` inside a string. Both fail CLOSED, the way this
# one did, so they cost a false flag rather than an unearned clear. Neither is
# reachable in the tree today, and neither is patched on speculation.
#
# Markers that excused nothing are reported by check_deprecated_endpoints
# rather than ignored, so a typo'd key or one left behind by a call that moved
# surfaces instead of sitting there looking like protection.
# Matches any marker, dated or not. Used for REPORTING, so an undated or
# expired one surfaces instead of reading as absent. Excusing needs a valid
# date, which is why the date group is optional here and required below.
# Two keywords, and the difference is what the marker CLAIMS, not how strongly
# it is worded (#152):
#
#   audit-allow    the call survives its sunset. Clears the finding.
#   audit-degrades the call is deliberate and does NOT survive its sunset. Does
#                  not clear anything; it names the tracker and lets the call go
#                  on being reported until it is deleted.
#
# audit-allow was carrying both meanings, which made it untrue at the one site
# using it: background.html's v1 probe is expected to fail after the sunset, and
# `found || !complete` collapses to a constant once it does, so the v2 answer is
# discarded rather than used in its place. A marker whose contract reads "every
# use is a claim that the call survives its sunset" cannot honestly cover that.
# NAMED groups, not numbered. #162 added the `kind` group and renumbered every
# group after it; four of the five consumers were updated and line 835 was not,
# so the stale-marker WARN reported the marker's KIND where it meant its KEY, on
# a check whose whole purpose is naming a typo'd key (#168). A numbered group is
# a positional contract whose consumers scatter away from the edit, and the
# comment that documented the numbering here went stale in the same commit: it
# still said "group 2 is a well-formed date" after the date had become group 3.
#
# `mk.group('key')` cannot silently become the kind, so this deletes the class
# rather than fixing this instance of it. Names also make the comment redundant,
# which is the point: the numbering no longer has to be documented to be read.
#
# `malformed` catches whatever else was written after `until`, so a malformed
# expiry is reported as one instead of collapsing into "no expiry" on a line that
# visibly says `until` (#159 review).
ALLOW_MARKER = re.compile(
    r"(?:(?<!:)//|#|/\*|<!--)[^\n]*?audit-(?P<kind>allow|degrades):\s*(?P<key>[\w.-]+)"
    r"(?:\s+until\s+(?:(?P<date>\d{4}-\d{2}-\d{2})(?!\d)|(?P<malformed>\S+)))?"
    r"(?:\s+tracked-by\s+(?P<tracker>\S+))?"
)


def _sunset_urgency(dates, today=None):
    """Days until the nearest sunset in `dates`, or None if there are none.

    `today` is a parameter so the escalation can be exercised against a
    synthetic date instead of by waiting for the calendar.
    """
    today = today or datetime.date.today()
    remaining = [(datetime.date.fromisoformat(d) - today).days for d in dates]
    return min(remaining) if remaining else None


# Every way this codebase could declare baseUrl. Keyed on the shape of a
# declaration rather than on one literal, for the same reason the fallback
# below is parsed rather than pattern-matched: the first version of this fixed
# the spelling of the DEFAULT and left the spelling of the DECLARATION matched
# against `function baseUrl(`, so an arrow-function rewrite turned the gate off
# and the assertion meant to catch that reported PASS (#163 review).
BASEURL_DECL = re.compile(
    r"(?:function\s+baseUrl\s*\("
    r"|\bbaseUrl\s*=\s*(?:async\s+)?(?:function\s*\*?\s*\(|\([^)]*\)\s*=>|[\w$]+\s*=>))"
)


def _baseurl_span(body):
    """(start, end) of a baseUrl declaration including its parameter list.

    The span starts at the declaration and ends at the close of its body, so
    the caller reads the parameter list AND the body. Reading only the body
    missed `function baseUrl(version = 'v1')`, which is the most idiomatic
    modern spelling of this exact default and reported as unreadable while
    sitting in plain sight (#163 review).

    The parameter list is paren-matched before the body brace is located, so a
    destructured parameter (`function baseUrl({ version } = {})`) does not put
    the scan inside the wrong braces.
    """
    m = BASEURL_DECL.search(body)
    if not m:
        return None
    cursor = m.start()
    paren = body.find("(", cursor)
    brace = body.find("{", cursor)
    if paren != -1 and (brace == -1 or paren < brace):
        depth = 0
        for k in range(paren, len(body)):
            if body[k] == "(":
                depth += 1
            elif body[k] == ")":
                depth -= 1
                if depth == 0:
                    cursor = k
                    break
        else:
            return None
    open_brace = body.find("{", cursor)
    if open_brace < 0:
        return None
    depth = 0
    for k in range(open_brace, len(body)):
        if body[k] == "{":
            depth += 1
        elif body[k] == "}":
            depth -= 1
            if depth == 0:
                return m.start(), k
    return None


def _baseurl_default(body):
    """The API version a file's baseUrl() falls back to, or None if unreadable.

    Read out of the declaration rather than off one spelling of the fallback.
    The rule this replaces matched the literal text `|| 'v1'`, so rewriting it
    as `(version ? version : 'v1')` or `(version ?? 'v1')` — same meaning, same
    default — turned needs_v1_base off and made every relative v1 call in the
    file invisible. Same blindness tsanetgit/Zendesk_App#148 was written to fix,
    one refactor away (tsanetgit/Zendesk_App#158).

    Returns None when the file has no baseUrl at all, and also when it has one
    whose default cannot be read. The caller treats those differently: no
    baseUrl is ordinary, an unreadable one is a finding, because a gate that
    cannot see its own anchor is not gating.
    """
    span = _baseurl_span(body)
    if span is None:
        return None
    # A quoted bare version token. The host literals in the same body are not
    # matched, because a quote has to sit immediately before the `v`.
    m = re.search(r"""['"`](v\d+)['"`]""", body[span[0]:span[1]])
    return m.group(1) if m else None


def _has_baseurl(body):
    return BASEURL_DECL.search(body) is not None


def _matches_unmarked(lines, pat, key, date, used=None, marked=None, today=None):
    """True when `pat` hits a call site that no `audit-allow: <key>` excuses.

    Matching is per call site rather than per file, so a marker clears the call
    it sits on and nothing else. `used` collects the (line, column) of every
    marker that was applied to a call, and `marked` collects
    (line_number, key, until) for every marked call site so the caller can
    report calls that are excused but still present. `date` is the entry's
    sunset, which decides whether a marker is still allowed to excuse at all.

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
                if mk.group("key") == key and mk.start() >= starts_here[0].end():
                    excuse = mk
                    break
        if excuse is not None:
            # Credit the marker as used even when it does not excuse, so an
            # expired or undated one is reported as the problem it is rather
            # than additionally as "excused nothing", which sends the author
            # chasing the wrong edit.
            if used is not None:
                used.add((i, excuse.start()))
            if marked is not None:
                marked.append((i + 1, excuse.group("kind"), excuse.group("key"),
                               excuse.group("date"), excuse.group("malformed"),
                               excuse.group("tracker")))
            if _excuses(excuse, key, date, today):
                continue
        hit = True
    return hit


def _excuses(marker, key, sunset, today=None):
    """Does this marker actually excuse a call to an entry sunsetting on `sunset`?

    Naming the right key and sitting in the right place is not enough (#151).
    A marker must also be dated and unexpired, and it stops excusing once the
    sunset is close enough that the suite would escalate, so the scheduled
    2026-10-03 FAIL cannot be deferred by writing a later date.
    """
    # Only audit-allow clears. audit-degrades is an annotation: it says the call
    # is deliberate AND that it does not survive, so the call keeps reporting.
    if (marker.group("kind") != "allow" or marker.group("key") != key
            or not marker.group("date")):
        return False
    today = today or datetime.date.today()
    try:
        until = datetime.date.fromisoformat(marker.group("date"))
    except ValueError:
        return False
    if until < today:
        return False
    days = (datetime.date.fromisoformat(sunset) - today).days
    return days > SUNSET_FAIL_WITHIN_DAYS


# The bundle has no module system: main.js and background.html each carry their
# own copy of the helpers they share. Some copies are contractually identical and
# some legitimately diverge, and the difference has to be recorded somewhere a
# machine reads. A comment cannot do it: addTicketTag shipped in #167 with the
# comment "Keep them identical; the two files have already drifted once on a
# shared helper's argument order", which is an acknowledged hazard controlled by
# prose (#168, Coder defect pattern 7).
#
# Classified rather than allowlisted. Every name defined in BOTH files must appear
# here, so a NEW duplicated helper fails until someone decides which class it is
# in; an allowlist of names to compare would only ever check the ones already
# thought of, which is how this class keeps reopening.
SHARED_HELPERS = {
    # name: (must_match, why)
    "addTicketTag": (True, "one tag-write contract; drift is how #165 came back"),
    "baseUrl": (True, "the v1-base anchor is read out of this, per #158/#163"),
    "getJwt": (False, "main.js surfaces login failure in the sidebar UI, "
                      "background.html logs it"),
    "tsanetGet": (False, "same signature since #161, but main.js reports errors "
                         "to the agent and background.html to the console"),
}
SHARED_HELPER_FILES = ("zaf-build/assets/main.js", "zaf-build/assets/background.html")
# If the extractor finds nothing it must FAIL, not pass. An empty intersection
# would otherwise satisfy every comparison vacuously — the same
# wrong-but-well-formed failure #160 records for a PASS line that reads
# "nothing found" while something is in the tree.
SHARED_HELPER_SENTINEL = "addTicketTag"


def _js_functions(text):
    """Map `function name(...) {...}` to its source span, brace-matched.

    Deliberately only handles the declaration form the bundle actually uses. A
    helper written as an arrow assignment would not be found, which is why the
    sentinel below exists: the check reports its own blindness instead of
    reporting clean.
    """
    out = {}
    for m in re.finditer(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(", text):
        open_brace = text.find("{", m.end() - 1)
        if open_brace < 0:
            continue
        depth = 0
        for j in range(open_brace, len(text)):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    out[m.group(1)] = text[m.start():j + 1]
                    break
    return out


def _norm_js(src):
    """Compare behaviour, not layout: strip comments, collapse whitespace.

    `(?<!:)` on the line-comment branch for the same reason ALLOW_MARKER carries
    it: without it the `//` in a URL literal starts a "comment" that swallows the
    rest of the line. Probed on this tree before the guard was added, changing
    `https://connect2.tsanet.org` to another host in one copy of baseUrl() read as
    IDENTICAL, because both sides normalised to `? 'https: : 'https:`. baseUrl is
    required to match and is where the v1-base anchor is read from, so the strip
    was blinding the check on the one line it most needs to see.
    """
    no_comments = re.sub(r"(?<!:)//[^\n]*|/\*.*?\*/", "", src, flags=re.S)
    return " ".join(no_comments.split())


def check_shared_helper_drift(root):
    cat = "supply-chain"
    name = "duplicated bundle helpers stay identical"
    try:
        defs = [_js_functions(read(root, f)) for f in SHARED_HELPER_FILES]
    except OSError as e:
        record("FAIL", name, str(e), cat)
        return

    shared = set(defs[0]) & set(defs[1])
    if SHARED_HELPER_SENTINEL not in shared:
        record("FAIL", name,
               f"the extractor did not find {SHARED_HELPER_SENTINEL}() in both "
               f"files, so it cannot be trusted to have found anything; a passing "
               f"comparison here would be vacuous", cat)
        return

    unclassified = sorted(shared - set(SHARED_HELPERS))
    # The other direction, which is not symmetric with the one above. A helper
    # required to match that stops being FOUND in both files drops out of
    # `shared` and would otherwise be silently unmonitored: rewriting baseUrl as
    # an arrow assignment in one file is itself drift, and _js_functions only
    # reads the declaration form. Classified-but-absent is therefore a finding,
    # not a pass.
    stale_class = sorted(n for n, (must, _) in SHARED_HELPERS.items()
                         if must and n not in shared)
    drifted = sorted(n for n in shared
                     if SHARED_HELPERS.get(n, (False,))[0]
                     and _norm_js(defs[0][n]) != _norm_js(defs[1][n]))

    if stale_class:
        record("FAIL", name,
               f"helper(s) required to stay identical are no longer found in both "
               f"files: {', '.join(stale_class)}. Either the declaration form "
               f"changed, which this check cannot read, or the helper moved; "
               f"neither is a pass", cat)
        return
    if unclassified:
        record("FAIL", name,
               f"helper(s) defined in both bundle files and classified in "
               f"neither direction: {', '.join(unclassified)}. Add each to "
               f"SHARED_HELPERS as identical or deliberately divergent, with the "
               f"reason", cat)
        return
    if drifted:
        record("FAIL", name,
               f"{len(drifted)} helper(s) declared identical have drifted between "
               f"{' and '.join(SHARED_HELPER_FILES)}: {', '.join(drifted)}", cat)
        return

    must = [n for n in sorted(shared) if SHARED_HELPERS[n][0]]
    may = [n for n in sorted(shared) if not SHARED_HELPERS[n][0]]
    record("PASS", name,
           f"{len(must)} helper(s) required identical and identical "
           f"({', '.join(must)}); {len(may)} deliberately divergent "
           f"({', '.join(may) or 'none'})", cat)


def check_deprecated_endpoints(root, scan, today=None):
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
    unreadable_anchor = []
    stale_markers = []
    bad_markers = []      # undated or expired: they excuse nothing
    still_marked = []     # excused, and the call is still in the tree
    superseded = []       # marked, but the escalation window withdrew the excuse
    degrading  = []       # audit-degrades: deliberate, and does NOT survive
    marked_dates = []
    # `scan.paths`, not a walk of `root`: a walk cannot tell this tree from a
    # second checkout inside it, and scanned both. See _tree_files().
    for rel in scan.paths:
        if not rel.endswith(SCANNED_SUFFIXES):
            continue
        # Files that DESCRIBE the deprecated endpoints rather than call
        # them. Without the audit-record exclusion this check can never
        # reach PASS again: the record permanently names the call sites a
        # migration removed, so fixing the finding would not clear it.
        if rel in (AUDIT_SCRIPT, ".security-audit.json"):
            continue
        try:
            body = read(root, rel)
        except OSError:
            continue
        # Two readings, because either alone has a hole. The regex catches
        # the old inline-host shape ('https://connect2.tsanet.net/v1'); the
        # parsed default catches every spelling of the parameterised one.
        default = _baseurl_default(body)
        on_v1_base = re.search(v1_base, body) is not None or default == "v1"
        if (rel.startswith("zaf-build" + os.sep) and _has_baseurl(body)
                and default is None and not on_v1_base):
            unreadable_anchor.append(rel)
        lines = body.splitlines()
        used = set()
        for pat, needs_v1_base, key, label, date, repl in deprecated:
            if needs_v1_base and not on_v1_base:
                continue
            marked = []
            unmarked = _matches_unmarked(
                lines, pat, key, date, used, marked, today)
            now = today or datetime.date.today()
            for lineno, kind, mkey, until, malformed, tracker in marked:
                kw = f"audit-{kind}"
                # audit-degrades never clears, so it needs no expiry logic to
                # decide anything. What it does need is the tracker, because
                # naming where the deletion is owed is its entire job
                # (#152, #153). Without one it is a shrug in a comment.
                if kind == "degrades":
                    if not tracker:
                        bad_markers.append(
                            f"{rel}:{lineno}: {kw}: {mkey} has no "
                            f"`tracked-by <issue>`, so it names no owner for "
                            f"the deletion it admits is owed")
                    else:
                        degrading.append(
                            f"{rel}:{lineno}: {label} (deliberate, does NOT "
                            f"survive its {date} sunset, tracked by {tracker})")
                    continue
                # Three different mistakes, three different messages. They
                # all FAIL and none excuses its call, but "has no expiry"
                # on a line that visibly reads `until soon` sends the author
                # looking for the wrong thing (#159 review).
                if not until and malformed:
                    bad_markers.append(
                        f"{rel}:{lineno}: {kw}: {mkey} has a malformed "
                        f"expiry {malformed!r}, expected "
                        f"`until <YYYY-MM-DD>`, so it excuses nothing")
                    continue
                if not until:
                    bad_markers.append(
                        f"{rel}:{lineno}: {kw}: {mkey} has no "
                        f"`until <YYYY-MM-DD>` expiry, so it excuses nothing")
                    continue
                # ALLOW_MARKER only checks the SHAPE of the date, so an
                # ordinary typo like 2026-02-30 reaches this parse. Left
                # unguarded it raised out of the whole audit: exit 3, no
                # finding, no file, no line, and the other checks never ran.
                # _excuses already guarded the same call; this caller did
                # not (#159 review).
                try:
                    expires = datetime.date.fromisoformat(until)
                except ValueError:
                    bad_markers.append(
                        f"{rel}:{lineno}: {kw}: {mkey} has an expiry "
                        f"of {until!r}, which is the right shape but not a "
                        f"real date, so it excuses nothing")
                    continue
                if expires < now:
                    bad_markers.append(
                        f"{rel}:{lineno}: {kw}: {mkey} expired {until}")
                    continue
                # A marker inside the escalation window has stopped
                # excusing (condition 6), so its call is already a finding
                # below. Reporting it here as "deliberate" too would put two
                # lines about one call in the same run saying opposite
                # things (#159 review).
                if (datetime.date.fromisoformat(date) - now).days <= SUNSET_FAIL_WITHIN_DAYS:
                    superseded.append(f"{rel}:{lineno}: {kw}: {mkey}")
                    continue
                still_marked.append(
                    f"{rel}:{lineno}: {label} (deliberate until {until}, "
                    f"sunset {date}, use {repl})")
                marked_dates.append(date)
            if unmarked:
                found.append(f"{rel}: {label} (sunset {date}, use {repl})")
                hit_dates.append(date)
        # A marker that excused nothing is not harmless. It reads in review
        # as a considered exemption while protecting nothing, and it is what
        # a typo'd key or a call that moved leaves behind.
        for i, ln in enumerate(lines):
            for mk in ALLOW_MARKER.finditer(ln):
                if (i, mk.start()) not in used:
                    # The key is what the author needs in order to find the
                    # typo, and the kind is what this reported instead until
                    # #168. Both are named now, and the prefix is derived
                    # from the match rather than hardcoded to `audit-allow`,
                    # so a stale `audit-degrades:` marker is not reported
                    # under the other vocabulary's name.
                    stale_markers.append(
                        f"{rel}:{i + 1}: audit-{mk.group('kind')}: "
                        f"{mk.group('key')}")

    # Assert the anchor rather than inferring it. needs_v1_base decides whether
    # a relative path counts as a v1 call, so a baseUrl() whose default cannot
    # be read silently switches that gating off for the whole file and every
    # relative v1 call in it goes unreported (tsanetgit/Zendesk_App#158).
    if unreadable_anchor:
        record("FAIL", "the v1-base anchor is readable in every bundle file",
               "baseUrl() present but its default version could not be read, so "
               "relative-path deprecation checks are silently disabled for: "
               + "; ".join(sorted(set(unreadable_anchor))), cat)
    else:
        record("PASS", "the v1-base anchor is readable in every bundle file",
               "every bundle baseUrl() declares a readable default version", cat)

    if stale_markers:
        record("WARN", "every audit-allow marker excuses a real call",
               "marker excused nothing — stale, misspelled key, or the call it "
               "named has moved: " + "; ".join(sorted(set(stale_markers))), cat)
    else:
        record("PASS", "every audit-allow marker excuses a real call",
               "no unused audit-allow markers", cat)

    # An undated or expired marker is a FAIL rather than a WARN: it looks like
    # a considered exemption in review while granting nothing, so the call it
    # sits on is flagged AND the marker is called out, and neither reading is
    # left to be inferred from the other.
    if bad_markers:
        record("FAIL", "every audit-allow marker is dated and current",
               "; ".join(sorted(set(bad_markers))), cat)
    else:
        record("PASS", "every audit-allow marker is dated and current",
               "no undated or expired audit-allow markers", cat)

    # A valid marker stops a call being a FINDING; it never stops it being
    # REPORTED. #151: without this the suite returned PASS at exit 0 with a
    # live v1 call in the shipped bundle, because the marked call left both
    # `found` and `hit_dates`.
    if degrading:
        record("WARN", "no calls that die at their sunset remain",
               f"{len(degrading)} deliberate call(s) that do NOT survive their sunset, "
               f"still reported as findings until deleted: " + "; ".join(sorted(set(degrading))), cat)
    else:
        record("PASS", "no calls that die at their sunset remain",
               "no audit-degrades markers in the tree", cat)

    if still_marked or superseded:
        parts = []
        if still_marked:
            days = _sunset_urgency(marked_dates, today)
            parts.append(
                f"{len(still_marked)} deliberate call(s) still in the tree, "
                f"nearest sunset in {days} days: " + "; ".join(sorted(set(still_marked))))
        if superseded:
            parts.append(
                f"{len(superseded)} marker(s) no longer excusing, because the sunset "
                f"is within {SUNSET_FAIL_WITHIN_DAYS} days; those calls are reported "
                f"as findings rather than as deliberate: " + "; ".join(sorted(set(superseded))))
        record("WARN", "no deliberate sunsetting calls remain", " | ".join(parts), cat)
    else:
        record("PASS", "no deliberate sunsetting calls remain",
               "no audit-allow-marked calls in the tree", cat)

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
        # Enumerated once and handed to both consumers, so the check that
        # asserts the scan is sound is asserting the list that was actually
        # scanned rather than a second one that could differ from it. Inside
        # the try because failing to enumerate is the audit failing to RUN
        # (exit 3), not a finding.
        scan = _tree_files(root)
        check_all_workflows_declare_permissions(root)
        check_workflow_permissions(root)
        check_workflow_expression_injection(root)
        check_action_pinning(root)
        check_sdk_sri(root, network=not args.no_network)
        check_no_embedded_secrets(root)
        check_secure_settings(root)
        check_shared_helper_drift(root)
        check_scanned_tree(root, scan)
        check_deprecated_endpoints(root, scan)
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
