"""Regression corpus for scripts/security-audit.py.

Every test here pins a behavior that shipped wrong once, or a fail-closed
property the audit's own comments declare. Test names carry the issue the
behavior came from, so a future red test points at its history instead of
at archaeology. The receipts live in the issues and in tsanet-ops's
docs/CODER_DEFECT_PATTERNS.md.

Two conventions, both load-bearing:

1. FIXTURE STRINGS ARE ASSEMBLED, NEVER SPELLED. The audit scans every
   tracked .py file, including this one, for endpoint calls, audit markers,
   version-default shapes, and credential-shaped literals. A fixture spelled
   out verbatim would make the real repository audit flag this test file.
   Every string with audit significance is therefore concatenated from
   fragments split inside the matchable token, and the meta test at the
   bottom runs the real checks over this file's own bytes so a regression in
   the convention fails here, at pytest time, not at the next release run.

2. Assertions pin (status, check name, one discriminating token of detail).
   Detail prose has churned in every review round; full-string matches would
   make this suite a change detector rather than regression protection. The
   check names are centralized below so a rename is one deliberate edit.

xfail(strict=True) marks OPEN findings: the test asserts the CORRECT
behavior and is expected to fail until the issue is fixed, at which point
the strict XPASS turns the suite red and forces promotion to a plain test.
"""
from __future__ import annotations

import datetime
import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

GIT = shutil.which("git")

REPO = pathlib.Path(__file__).resolve().parents[1]
AUDIT_PATH = REPO / "scripts" / "security-audit.py"


def _load(path: pathlib.Path, name: str):
    """Load a script by path without leaving bytecode next to it."""
    prev = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        prev, sys.dont_write_bytecode = sys.dont_write_bytecode, prev


audit = _load(AUDIT_PATH, "security_audit")

# ── check names, centralized (assertion convention 2) ───────────────────
DEP_CHECK = "no calls to sunsetting endpoints"
READ_CHECK = "every file in deprecation scope was read"
ANCHOR_CHECK = "the v1-base anchor is readable in every bundle file"
STALE_CHECK = "every audit-" + "allow marker excuses a real call"
DATED_CHECK = "every audit-" + "allow marker is dated and current"
DEGRADES_CHECK = "no calls that die at their sunset remain"
DELIBERATE_CHECK = "no deliberate sunsetting calls remain"
TREE_CHECK = "the scanned tree is exactly this tree"
DRIFT_CHECK = "duplicated bundle helpers stay identical"
SECRETS_CHECK = "no embedded credentials anywhere in the repository"

# ── assembled fragments (fixture convention 1) ───────────────────────────
V1 = "v" + "1"
V2 = "v" + "2"
OR2 = "|" + "|"                       # never spell the JS or-operator pair
BASEURL = "base" + "Url"
WH_REL = "/web" + "hooks"             # relative endpoint path
COLLAB = "/collaboration-" + "requests?"
ALLOW = "audit-" + "allow"
DEGRADES = "audit-" + "degrades"
HOST = "connect2.tsanet." + "org"


def baseurl_fn(default=V1):
    """function baseUrl(version) { ... (version || '<default>') ... }"""
    return ("function " + BASEURL + "(version) { return 'https://" + HOST
            + "' + '/' + (version " + OR2 + " '" + default + "'); }")


def baseurl_default_param(default=V1):
    return ("function " + BASEURL + "(version = '" + default
            + "') { return 'https://" + HOST + "' + '/' + version; }")


def marker(kind=None, key="v1-webhooks", until=None, tracker=None):
    s = "// " + (kind or ALLOW) + ": " + key
    if until:
        s += " until " + until
    if tracker:
        s += " tracked-by " + tracker
    return s


def rel_call(version=None):
    call = "tsanetGet('" + WH_REL + "'"
    if version:
        call += ", '" + version + "'"
    return call + ")"


JAN1 = datetime.date(2026, 1, 1)      # 365 days before the 2027-01-01 sunset
OCT15 = datetime.date(2026, 10, 15)   # 78 days before it: inside the window


@pytest.fixture(autouse=True)
def _clean_results():
    audit.RESULTS.clear()
    yield
    audit.RESULTS.clear()


def by_check():
    return {r["check"]: r for r in audit.RESULTS}


def tree(tmp_path, files):
    """Write a fixture tree and hand back (root, Scan) the checks accept."""
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, bytes):
            p.write_bytes(body)
        else:
            p.write_text(body)
    paths = sorted(str(pathlib.PurePosixPath(r)).replace("/", os.sep)
                   for r in files)
    return str(tmp_path), audit.Scan(paths, "fixture", False)


# ── ALLOW_MARKER: the named-group representation (#162 → #168 → #170) ───

def test_marker_regex_uses_named_groups_170():
    """#162 renumbered positional groups and one consumer went stale (#168).
    Named groups delete the class; this pins the representation."""
    assert {"kind", "key", "date", "malformed", "tracker"} <= set(
        audit.ALLOW_MARKER.groupindex)


def test_marker_parses_every_field():
    m = audit.ALLOW_MARKER.search(
        marker(until="2027-01-01", tracker="#101"))
    assert m.group("kind") == "allow"
    assert m.group("key") == "v1-webhooks"
    assert m.group("date") == "2027-01-01"
    assert m.group("tracker") == "#101"
    assert m.group("malformed") is None


def test_marker_malformed_expiry_is_captured_not_dropped_159():
    m = audit.ALLOW_MARKER.search(marker(until="soon"))
    assert m.group("date") is None
    assert m.group("malformed") == "soon"


# ── _excuses: the six admission conditions (#151, #152) ──────────────────

def _mk(line):
    return audit.ALLOW_MARKER.search(line)


def test_degrades_never_excuses_152():
    m = _mk(marker(kind=DEGRADES, until="2026-12-31", tracker="#101"))
    assert audit._excuses(m, "v1-webhooks", "2027-01-01", today=JAN1) is False


def test_undated_marker_excuses_nothing_151():
    assert audit._excuses(_mk(marker()), "v1-webhooks", "2027-01-01",
                          today=JAN1) is False


def test_expired_marker_excuses_nothing_151():
    m = _mk(marker(until="2025-12-31"))
    assert audit._excuses(m, "v1-webhooks", "2027-01-01", today=JAN1) is False


def test_escalation_window_withdraws_the_excuse_151():
    """Condition 6: within SUNSET_FAIL_WITHIN_DAYS a marker stops excusing
    no matter what its expiry says, so the scheduled FAIL cannot be
    deferred by writing a later date."""
    m = _mk(marker(until="2099-01-01"))
    assert audit._excuses(m, "v1-webhooks", "2027-01-01", today=OCT15) is False
    assert audit._excuses(m, "v1-webhooks", "2027-01-01", today=JAN1) is True


# ── _matches_unmarked: grant mechanics ───────────────────────────────────

PAT_REL = r"""['"`]/webhooks['"`](?!\s*,\s*['"`]v2['"`])"""


def test_wrapped_migrated_call_is_not_flagged():
    """The two-line window exists so a call that wraps its version argument
    is not misread as the deprecated form (whose predictable 'fix' is a
    marker asserting something false)."""
    lines = [rel_call()[:-1] + ",", "          '" + V2 + "')"]
    assert audit._matches_unmarked(lines, PAT_REL, "v1-webhooks",
                                   "2027-01-01", today=JAN1) is False


def test_marker_on_the_previous_line_is_not_a_grant():
    lines = [marker(until="2026-12-31"), rel_call()]
    assert audit._matches_unmarked(lines, PAT_REL, "v1-webhooks",
                                   "2027-01-01", today=JAN1) is True


def test_marker_ahead_of_the_call_on_the_same_line_is_not_a_grant():
    lines = ["/* " + ALLOW + ": v1-webhooks until 2026-12-31 */ " + rel_call()]
    assert audit._matches_unmarked(lines, PAT_REL, "v1-webhooks",
                                   "2027-01-01", today=JAN1) is True


def test_two_calls_one_marker_flags_the_line():
    lines = [rel_call() + "; " + rel_call() + " " + marker(until="2026-12-31")]
    assert audit._matches_unmarked(lines, PAT_REL, "v1-webhooks",
                                   "2027-01-01", today=JAN1) is True


def test_non_excusing_marker_is_still_credited_as_used():
    """An expired marker must be reported as expired, not additionally as
    'excused nothing' — two contradictory findings for one edit."""
    used, marked = set(), []
    lines = [rel_call() + " " + marker(until="2025-01-01")]
    hit = audit._matches_unmarked(lines, PAT_REL, "v1-webhooks",
                                  "2027-01-01", used, marked, today=JAN1)
    assert hit is True and len(used) == 1 and len(marked) == 1


# ── baseUrl parsing: the anchor (#148 → #158 → #163, #164 open) ─────────

def test_decl_shapes_are_all_seen_163():
    arrow = BASEURL + " = (version) => 'x/' + (version " + OR2 + " '" + V1 + "')"
    bare_arrow = BASEURL + " = version => 'x/' + version"
    fn_expr = BASEURL + " = function (version) { return version; }"
    for body in (baseurl_fn(), arrow, bare_arrow, fn_expr):
        assert audit._has_baseurl(body), body
    assert not audit._has_baseurl("function otherHelper(v) { return v; }")


@pytest.mark.parametrize("body,want", [
    (baseurl_fn(V1), V1),
    (baseurl_fn(V2), V2),
    (baseurl_default_param(V1), V1),   # the spelling #163's review missed
    ("function " + BASEURL + "(v) { return h + '/' + (v ? v : '" + V1 + "'); }", V1),
    ("function " + BASEURL + "(v) { return h + '/' + (v ?? '" + V1 + "'); }", V1),
    ("function helper() {}", None),    # no declaration at all
])
def test_default_is_read_from_the_declaration_not_one_spelling_158(body, want):
    assert audit._baseurl_default(body) == want


@pytest.mark.xfail(strict=True, reason="tsanetgit/Zendesk_App#164: the "
                   "default is the first quoted version token in the span, "
                   "so a comment naming another version shadows the real "
                   "fallback; fixing #164 should XPASS this and force "
                   "promotion to a plain test")
def test_comment_token_inside_the_span_is_not_the_default_164():
    body = ("function " + BASEURL + "(version) { /* move to '" + V2
            + "' in Q3 */ return 'https://" + HOST + "' + '/' + (version "
            + OR2 + " '" + V1 + "'); }")
    assert audit._baseurl_default(body) == V1


def test_sunset_urgency_math():
    assert audit._sunset_urgency([], today=JAN1) is None
    assert audit._sunset_urgency(["2027-01-01", "2026-02-01"],
                                 today=JAN1) == 31


# ── shared-helper machinery (#161, #165, #167 → Pattern 7) ───────────────

def test_norm_js_does_not_read_url_slashes_as_a_comment():
    """Probed blindness: stripping '//' without the (?<!:) guard normalised
    two different hosts to the same string, on the one helper the v1-base
    anchor is read from."""
    src = "return 'https:" + "//" + HOST + "'; " + "//" + " trailing"
    out = audit._norm_js(src)
    assert HOST in out
    assert "trailing" not in out
    assert "gone" not in audit._norm_js("a = 1; /* gone */ b = 2;")


def test_js_functions_brace_matching_captures_nested_bodies():
    src = ("function a(x) { if (x) { return { k: 1 }; } } "
           "function b() {}")
    fns = audit._js_functions(src)
    assert set(fns) == {"a", "b"}
    assert "k: 1" in fns["a"]


ADD_TAG = "function addTicketTag(id, tag) { return client.request(id, tag); }"


def test_undecodable_page_does_not_abort_the_sri_check_197(tmp_path):
    """The other pre-#197 abort site. check_sdk_sri reads .html, which is in
    SCANNED_SUFFIXES, and runs at main():1589 — before the deprecation scan."""
    tree(tmp_path, {"zaf-build/assets/background.html": b"\xff\xfe\x00broken"})
    audit.check_sdk_sri(str(tmp_path), network=False)
    hits = [r for r in audit.RESULTS if r["check"].startswith("SDK integrity")]
    assert hits, "the check recorded nothing, so it did not run"
    assert all(h["status"] == "FAIL" for h in hits)
    assert any("background.html" in h["check"] for h in hits)


def _bundle(tmp_path, main_extra="", bg_extra="", main_base=None, bg_base=None):
    return tree(tmp_path, {
        "zaf-build/assets/main.js":
            ADD_TAG + "\n" + (main_base or baseurl_fn()) + "\n" + main_extra,
        "zaf-build/assets/background.html":
            ADD_TAG + "\n" + (bg_base or baseurl_fn()) + "\n" + bg_extra,
    })


def test_identical_required_helpers_pass(tmp_path):
    root, _ = _bundle(tmp_path)
    audit.check_shared_helper_drift(root)
    assert by_check()[DRIFT_CHECK]["status"] == "PASS"


def test_drifted_required_helper_fails_165(tmp_path):
    other = baseurl_fn().replace(HOST, "connect2.tsanet." + "net")
    root, _ = _bundle(tmp_path, bg_base=other)
    audit.check_shared_helper_drift(root)
    r = by_check()
    assert r[DRIFT_CHECK]["status"] == "FAIL"
    assert "drifted" in r[DRIFT_CHECK]["detail"]


def test_undecodable_bundle_js_does_not_abort_the_drift_check_197(tmp_path):
    """#197 as it reaches THIS check. main() runs the drift check two checks
    before the deprecation scan, so an undecodable main.js aborted the whole
    audit here and the #197 fix downstream never got a chance to run."""
    root, _ = tree(tmp_path, {
        "zaf-build/assets/main.js": b"\xff\xfe\x00broken",
        "zaf-build/assets/background.html": ADD_TAG + "\n" + baseurl_fn(),
    })
    audit.check_shared_helper_drift(root)
    r = by_check()[DRIFT_CHECK]
    assert r["status"] == "FAIL"
    # The file, not just a byte offset: the comprehension this replaced
    # reported str(e) alone, which names a position in nothing.
    assert "main.js" in r["detail"]


def test_new_shared_helper_must_be_classified(tmp_path):
    extra = "function newThing() { return 1; }"
    root, _ = _bundle(tmp_path, main_extra=extra, bg_extra=extra)
    audit.check_shared_helper_drift(root)
    r = by_check()[DRIFT_CHECK]
    assert r["status"] == "FAIL" and "newThing" in r["detail"]


def test_missing_sentinel_is_vacuity_not_a_pass_160(tmp_path):
    root, _ = tree(tmp_path, {
        "zaf-build/assets/main.js": baseurl_fn(),
        "zaf-build/assets/background.html": baseurl_fn(),
    })
    audit.check_shared_helper_drift(root)
    r = by_check()[DRIFT_CHECK]
    assert r["status"] == "FAIL" and "vacuous" in r["detail"]


def test_required_helper_leaving_one_file_is_drift_not_silence(tmp_path):
    arrow = BASEURL + " = (v) => 'https://" + HOST + "/' + (v " + OR2 + " '" + V1 + "');"
    root, _ = _bundle(tmp_path, bg_base=arrow)
    audit.check_shared_helper_drift(root)
    r = by_check()[DRIFT_CHECK]
    assert r["status"] == "FAIL" and "no longer found in both" in r["detail"]


# ── check_deprecated_endpoints end-to-end (#144, #145, #151, #152) ──────

def test_unmarked_relative_call_on_v1_base_is_a_finding_144(tmp_path):
    root, scan = tree(tmp_path, {
        "zaf-build/assets/main.js": baseurl_fn() + "\n" + rel_call()})
    audit.check_deprecated_endpoints(root, scan, today=JAN1)
    r = by_check()[DEP_CHECK]
    assert r["status"] == "WARN" and "webhook" in r["detail"]


def test_migrating_the_default_clears_relative_calls_148(tmp_path):
    """The design premise: a relative path counts only against a v1 base,
    so migrating the base clears the finding instead of flagging forever."""
    root, scan = tree(tmp_path, {
        "zaf-build/assets/main.js": baseurl_fn(V2) + "\n" + rel_call()})
    audit.check_deprecated_endpoints(root, scan, today=JAN1)
    assert by_check()[DEP_CHECK]["status"] == "PASS"


def test_absolute_v1_call_needs_no_base_context_144(tmp_path):
    root, scan = tree(tmp_path, {
        "sync.py": "requests.get(f'{ts}" + "/" + V1 + WH_REL + "')"})
    audit.check_deprecated_endpoints(root, scan, today=JAN1)
    assert by_check()[DEP_CHECK]["status"] == "WARN"


def test_html_call_sites_are_scanned_145(tmp_path):
    """#144 read as a single-site problem for four releases because .html
    was not scanned; background.html held three of the four call sites."""
    root, scan = tree(tmp_path, {
        "zaf-build/assets/background.html":
            "<script>" + baseurl_fn() + "\n" + rel_call() + "</script>"})
    audit.check_deprecated_endpoints(root, scan, today=JAN1)
    assert by_check()[DEP_CHECK]["status"] == "WARN"


def test_unscanned_suffix_stays_out_of_scope(tmp_path):
    root, scan = tree(tmp_path, {
        "notes.md": baseurl_fn() + "\n" + rel_call()})
    audit.check_deprecated_endpoints(root, scan, today=JAN1)
    assert by_check()[DEP_CHECK]["status"] == "PASS"


def test_the_audit_script_itself_is_excluded(tmp_path):
    """The audit's own pattern definitions must not read as call sites."""
    root, scan = tree(tmp_path, {
        audit.AUDIT_SCRIPT: baseurl_fn() + "\n" + rel_call()})
    audit.check_deprecated_endpoints(root, scan, today=JAN1)
    assert by_check()[DEP_CHECK]["status"] == "PASS"


def test_marked_call_is_cleared_but_still_reported_151(tmp_path):
    """#151's exact hole: a valid marker cleared the finding AND the
    report, so the suite said all-clear with a live v1 call shipping."""
    root, scan = tree(tmp_path, {
        "zaf-build/assets/main.js":
            baseurl_fn() + "\n" + rel_call() + "  " + marker(until="2026-12-31")})
    audit.check_deprecated_endpoints(root, scan, today=JAN1)
    r = by_check()
    assert r[DEP_CHECK]["status"] == "PASS"
    assert r[DELIBERATE_CHECK]["status"] == "WARN"
    assert "v1-webhooks" not in r[STALE_CHECK]["detail"]


def test_escalation_supersedes_the_marker_and_fails_151(tmp_path):
    root, scan = tree(tmp_path, {
        "zaf-build/assets/main.js":
            baseurl_fn() + "\n" + rel_call() + "  " + marker(until="2099-01-01")})
    audit.check_deprecated_endpoints(root, scan, today=OCT15)
    r = by_check()
    assert r[DEP_CHECK]["status"] == "FAIL"
    assert "threshold" in r[DEP_CHECK]["detail"]
    warn = r[DELIBERATE_CHECK]["detail"]
    assert "no longer excusing" in warn
    # #159's double-report shape: one call must not be listed as deliberate
    # AND as a finding in the same run.
    assert "still in the tree" not in warn


def test_degrades_is_annotated_and_stays_a_finding_152(tmp_path):
    root, scan = tree(tmp_path, {
        "zaf-build/assets/main.js":
            baseurl_fn() + "\n" + rel_call() + "  "
            + marker(kind=DEGRADES, tracker="#101")})
    audit.check_deprecated_endpoints(root, scan, today=JAN1)
    r = by_check()
    assert r[DEGRADES_CHECK]["status"] == "WARN"
    assert "#101" in r[DEGRADES_CHECK]["detail"]
    assert r[DEP_CHECK]["status"] == "WARN"


def test_degrades_without_a_tracker_fails_153(tmp_path):
    root, scan = tree(tmp_path, {
        "zaf-build/assets/main.js":
            baseurl_fn() + "\n" + rel_call() + "  " + marker(kind=DEGRADES)})
    audit.check_deprecated_endpoints(root, scan, today=JAN1)
    r = by_check()[DATED_CHECK]
    assert r["status"] == "FAIL" and "tracked-by" in r["detail"]


def test_stale_marker_warn_names_the_key_168(tmp_path):
    """The #168 regression: the WARN printed the marker's KIND where it
    meant its KEY, on the check whose whole job is naming a typo'd key."""
    root, scan = tree(tmp_path, {
        "zaf-build/assets/main.js":
            baseurl_fn() + "\n" + marker(key="v1-typo-key", until="2026-12-31")})
    audit.check_deprecated_endpoints(root, scan, today=JAN1)
    r = by_check()[STALE_CHECK]
    assert r["status"] == "WARN" and "v1-typo-key" in r["detail"]


@pytest.mark.parametrize("until,token", [
    (None, "no `until"),
    ("soon", "malformed expiry"),
    ("2026-02-30", "not a real date"),
    ("2025-06-01", "expired"),
])
def test_bad_markers_fail_with_the_right_message_159(tmp_path, until, token):
    """Three different mistakes, three different messages — and the
    shape-valid impossible date must be a finding, not an audit abort."""
    root, scan = tree(tmp_path, {
        "zaf-build/assets/main.js":
            baseurl_fn() + "\n" + rel_call() + "  " + marker(until=until)})
    audit.check_deprecated_endpoints(root, scan, today=JAN1)
    r = by_check()
    assert r[DATED_CHECK]["status"] == "FAIL"
    assert token in r[DATED_CHECK]["detail"]
    assert r[DEP_CHECK]["status"] == "WARN"   # the call itself still flags


def test_unreadable_anchor_in_the_bundle_fails_158(tmp_path):
    body = ("function " + BASEURL
            + "(version) { return HOSTS[env] + '/' + version; }")
    root, scan = tree(tmp_path, {"zaf-build/assets/main.js": body})
    audit.check_deprecated_endpoints(root, scan, today=JAN1)
    assert by_check()[ANCHOR_CHECK]["status"] == "FAIL"


def test_unreadable_anchor_outside_the_bundle_is_ordinary(tmp_path):
    body = ("function " + BASEURL
            + "(version) { return HOSTS[env] + '/' + version; }")
    root, scan = tree(tmp_path, {"scripts/tool.py": body})
    audit.check_deprecated_endpoints(root, scan, today=JAN1)
    assert by_check()[ANCHOR_CHECK]["status"] == "PASS"


def test_one_undecodable_file_does_not_abort_the_scan_197(tmp_path):
    root, scan = tree(tmp_path, {
        "zaf-build/assets/main.js": baseurl_fn() + "\n" + rel_call(),
        "zaf-build/assets/bad.js": b"\xff\xfe\x00broken",
    })
    audit.check_deprecated_endpoints(root, scan, today=JAN1)
    assert by_check()[DEP_CHECK]["status"] == "WARN"


def test_undecodable_file_is_named_and_fails_its_own_check_197(tmp_path):
    """The abort is gone, but the file is still unscanned. Silence there
    would trade a loud wrong answer for a quiet one: exit 3 flags coverage
    in release.yml today, so anything short of FAIL opens that gate."""
    root, scan = tree(tmp_path, {
        "zaf-build/assets/main.js": baseurl_fn() + "\n" + rel_call(),
        "zaf-build/assets/bad.js": b"\xff\xfe\x00broken",
    })
    audit.check_deprecated_endpoints(root, scan, today=JAN1)
    r = by_check()[READ_CHECK]
    assert r["status"] == "FAIL"
    assert "bad.js" in r["detail"]          # named, not just counted
    assert "1 read" in r["detail"]          # and the buckets reconcile
    assert "of 2 in scope" in r["detail"]


def test_every_file_readable_passes_with_a_tally_197(tmp_path):
    root, scan = tree(tmp_path, {
        "zaf-build/assets/main.js": baseurl_fn() + "\n" + rel_call()})
    audit.check_deprecated_endpoints(root, scan, today=JAN1)
    assert by_check()[READ_CHECK]["status"] == "PASS"


def test_the_read_record_survives_the_no_findings_return_197(tmp_path):
    """The record sits BEFORE `if not found: return`. Behind it, a tree with
    nothing deprecated in it reports no record at all and the run exits 0 —
    the loosened gate the severity choice exists to prevent. Every other test
    in this group uses a fixture that HAS a finding, so none of them would
    notice the block being moved."""
    root, scan = tree(tmp_path, {
        "zaf-build/assets/clean.js": "let x = 1;",
        "zaf-build/assets/bad.js": b"\xff\xfe\x00broken",
    })
    audit.check_deprecated_endpoints(root, scan, today=JAN1)
    r = by_check()
    assert r[DEP_CHECK]["status"] == "PASS"       # nothing deprecated found
    assert r[READ_CHECK]["status"] == "FAIL"      # and the skip still reports
    assert "bad.js" in r[READ_CHECK]["detail"]


def test_both_buckets_are_named_not_just_counted_197(tmp_path):
    """Reported as if/elif, the undecodable FAIL swallowed the unreadable
    list: the tally said 1 unreadable and nothing said which file."""
    root, scan = tree(tmp_path, {
        "zaf-build/assets/main.js": baseurl_fn() + "\n" + rel_call(),
        "zaf-build/assets/bad.js": b"\xff\xfe\x00broken",
    })
    scan = audit.Scan(scan.paths + ["zaf-build/assets/gone.js"],
                      scan.source, scan.degraded)
    audit.check_deprecated_endpoints(root, scan, today=JAN1)
    r = by_check()[READ_CHECK]
    assert r["status"] == "FAIL"            # undecodable still sets severity
    assert "bad.js" in r["detail"]
    assert "gone.js" in r["detail"]         # and the WARN bucket is not lost
    assert "of 3 in scope" in r["detail"]


def test_unopenable_file_warns_rather_than_passing_197(tmp_path):
    """The pre-existing `except OSError: continue` dropped these silently.
    WARN is the tightening; FAIL would move the release gate for a dirty
    working tree rather than for a defect in the code under review."""
    root, scan = tree(tmp_path, {
        "zaf-build/assets/main.js": baseurl_fn() + "\n" + rel_call()})
    scan = audit.Scan(scan.paths + ["zaf-build/assets/gone.js"],
                      scan.source, scan.degraded)
    audit.check_deprecated_endpoints(root, scan, today=JAN1)
    r = by_check()[READ_CHECK]
    assert r["status"] == "WARN"
    assert "gone.js" in r["detail"]


# ── enumeration: _tree_files / check_scanned_tree (#184, #185, #193-195) ─

def test_plain_directory_takes_the_walk_route_184(tmp_path):
    (tmp_path / "a.js").write_text("let x = 1;")
    scan = audit._tree_files(str(tmp_path))
    assert "not a git repository" in scan.source
    assert scan.degraded is False
    assert scan.paths == ["a.js"]


def test_walk_prunes_a_nested_checkout_by_its_git_entry_184(tmp_path):
    (tmp_path / "a.js").write_text("x")
    nested = tmp_path / "vendor"
    nested.mkdir()
    (nested / ".git").write_text("gitdir: elsewhere")   # worktrees use a FILE
    (nested / "b.js").write_text("y")
    scan = audit._tree_files(str(tmp_path))
    assert scan.paths == ["a.js"]


@pytest.mark.skipif(GIT is None, reason="git not on PATH")
def test_git_route_reads_the_index_not_the_directory_185(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "tracked.js").write_text("x")
    (tmp_path / "untracked.js").write_text("y")
    subprocess.run(["git", "-C", str(tmp_path), "add", "tracked.js"],
                   check=True)
    scan = audit._tree_files(str(tmp_path))
    assert scan.source == "git ls-files"
    assert "tracked.js" in scan.paths
    assert "untracked.js" not in scan.paths     # #185's whole point


@pytest.mark.skipif(GIT is None, reason="git not on PATH")
def test_export_inside_an_ignoring_ancestor_is_fully_enumerated(tmp_path):
    """The ancestor-repo gate: git SUCCEEDS for an export unpacked inside
    some other checkout and silently applies that repo's ignore rules. The
    .git-exists gate routes the export to the walk instead."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("*.json\n")
    export = tmp_path / "export"
    export.mkdir()
    (export / "manifest.json").write_text("{}")
    scan = audit._tree_files(str(export))
    assert "not a git repository" in scan.source
    assert "manifest.json" in scan.paths


def test_conflicted_index_duplicates_are_deduplicated_194(tmp_path, monkeypatch):
    """--cached emits an unmerged path once per stage; the raw list made
    the sentinel guard report a second checkout that did not exist and let
    counts disagree with their own detail lines."""
    (tmp_path / ".git").mkdir()
    sentinel = audit.AUDIT_SCRIPT.replace(os.sep, "/").encode()
    out = b"\0".join([sentinel, sentinel, sentinel, b"a.js"]) + b"\0"

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr=b"")

    monkeypatch.setattr(audit.subprocess, "run", fake_run)
    scan = audit._tree_files(str(tmp_path))
    assert scan.paths.count(audit.AUDIT_SCRIPT) == 1
    audit.check_scanned_tree(str(tmp_path), scan)
    assert by_check()[TREE_CHECK]["status"] == "PASS"


def test_degraded_enumeration_says_why_without_crashing(tmp_path, monkeypatch):
    """git's stderr is a message, not a path: a non-UTF-8 byte in it must
    render as U+FFFD instead of raising when the report prints."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "a.js").write_text("x")

    def fake_run(cmd, **kw):
        raise subprocess.CalledProcessError(128, cmd,
                                            stderr=b"\xfffatal: boom")

    monkeypatch.setattr(audit.subprocess, "run", fake_run)
    scan = audit._tree_files(str(tmp_path))
    assert scan.degraded is True
    assert "fatal: boom" in scan.source
    scan.source.encode("utf-8")     # printable, no surrogates


def test_missing_sentinel_fails_the_tree_check(tmp_path):
    audit.check_scanned_tree(str(tmp_path),
                             audit.Scan(["a.js"], "fixture", False))
    r = by_check()[TREE_CHECK]
    assert r["status"] == "FAIL" and "not listing" in r["detail"]


def test_duplicate_sentinel_fails_as_a_second_checkout_193(tmp_path):
    twin = os.path.join(".claude", "worktrees", "x", audit.AUDIT_SCRIPT)
    audit.check_scanned_tree(
        str(tmp_path),
        audit.Scan([audit.AUDIT_SCRIPT, twin], "fixture", False))
    r = by_check()[TREE_CHECK]
    assert r["status"] == "FAIL" and "second checkout" in r["detail"]


def test_paths_inside_a_nested_checkout_fail_184(tmp_path):
    nested = tmp_path / "other"
    nested.mkdir()
    (nested / ".git").mkdir()
    (nested / "x.js").write_text("x")
    audit.check_scanned_tree(
        str(tmp_path),
        audit.Scan([audit.AUDIT_SCRIPT, os.path.join("other", "x.js")],
                   "fixture", False))
    r = by_check()[TREE_CHECK]
    assert r["status"] == "FAIL" and "nested checkout" in r["detail"]


def test_directory_entries_fail_as_unscanned_content(tmp_path):
    (tmp_path / "subdir").mkdir()
    audit.check_scanned_tree(
        str(tmp_path),
        audit.Scan([audit.AUDIT_SCRIPT, "subdir"], "fixture", False))
    r = by_check()[TREE_CHECK]
    assert r["status"] == "FAIL" and "unscanned" in r["detail"]


def test_degraded_scan_warns_instead_of_passing_quietly(tmp_path):
    audit.check_scanned_tree(
        str(tmp_path),
        audit.Scan([audit.AUDIT_SCRIPT], "filesystem walk (degraded)", True))
    assert by_check()[TREE_CHECK]["status"] == "WARN"


# ── check_no_embedded_secrets (#196) ─────────────────────────────────────

GHP = "ghp" + "_" + "A" * 24
SECRET_ASSIGN = "client_" + 'secret: "supersecret' + "value12345" + '"'


def test_token_literal_fails_wherever_it_sits_196(tmp_path):
    """#196's control probe: the same literal PASSed in scripts/ while the
    scan only read zaf-build/. The whole enumeration is in scope now."""
    root, scan = tree(tmp_path, {"scripts/rotate.py": "t = '" + GHP + "'"})
    audit.check_no_embedded_secrets(root, scan)
    r = by_check()[SECRETS_CHECK]
    assert r["status"] == "FAIL" and "GitHub PAT" in r["detail"]


def test_assignment_hit_teaches_the_dollar_name_convention(tmp_path):
    root, scan = tree(tmp_path, {"docs.js": SECRET_ASSIGN})
    audit.check_no_embedded_secrets(root, scan)
    r = by_check()[SECRETS_CHECK]
    assert r["status"] == "FAIL" and "$NAME" in r["detail"]


def test_prefix_token_hit_skips_the_remedy_it_cannot_use(tmp_path):
    root, scan = tree(tmp_path, {"scripts/rotate.py": "t = '" + GHP + "'"})
    audit.check_no_embedded_secrets(root, scan)
    assert "$NAME" not in by_check()[SECRETS_CHECK]["detail"]


def test_tally_buckets_add_up_to_the_enumeration(tmp_path):
    root, scan = tree(tmp_path, {
        "ok.js": "let x = 1;",
        "image.png": b"\x89PNG\xff\xfe",
    })
    scan = audit.Scan(scan.paths + ["ghost.js"], "fixture", False)
    audit.check_no_embedded_secrets(root, scan)
    r = by_check()[SECRETS_CHECK]
    assert r["status"] == "WARN"                      # unreadable ≠ clean
    assert "could not be opened" in r["detail"]
    assert "1 read" in r["detail"]
    assert "of 3 enumerated" in r["detail"]


def test_nothing_readable_is_a_fail_not_a_pass(tmp_path):
    root, scan = tree(tmp_path, {"image.png": b"\x89PNG\xff\xfe"})
    audit.check_no_embedded_secrets(root, scan)
    r = by_check()[SECRETS_CHECK]
    assert r["status"] == "FAIL" and "could not run" in r["detail"]


def test_clean_tree_passes_with_a_reconciling_tally(tmp_path):
    root, scan = tree(tmp_path, {"ok.js": "let x = 1;"})
    audit.check_no_embedded_secrets(root, scan)
    r = by_check()[SECRETS_CHECK]
    assert r["status"] == "PASS" and "of 1 enumerated" in r["detail"]


# ── meta: this file is invisible to the audit it tests ──────────────────

def test_meta_this_file_trips_none_of_the_audit_patterns():
    """Enforces fixture convention 1 at pytest time. If an assembled
    fragment is ever 'simplified' back into a spelled-out literal, the
    real checks running over this file's own bytes catch it here, not in
    the next release run."""
    rel = os.path.relpath(__file__, REPO)
    scan = audit.Scan([rel], "meta", False)
    audit.check_deprecated_endpoints(str(REPO), scan, today=JAN1)
    bad = [r for r in audit.RESULTS if r["status"] != "PASS"]
    assert not bad, bad
    audit.RESULTS.clear()
    audit.check_no_embedded_secrets(str(REPO), scan)
    bad = [r for r in audit.RESULTS if r["status"] != "PASS"]
    assert not bad, bad
