"""Tests for scripts/audit-record-fields.py, the release gate's record reader.

Every case runs the real script as a subprocess against a synthetic record in
a temporary directory, which is how release.yml and tests.yml call it. The
not_verified cases are tsanetgit/Zendesk_App#140's acceptance: demonstrated on
synthetic records, not asserted from reading the code.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "audit-record-fields.py"


def nv(nid, status="open", **extra):
    e = {"id": nid, "summary": "a claim not checked", "first_seen": "review X",
         "status": status}
    e.update(extra)
    return e


def record(entries=(), **extra):
    r = {"reviewed_commit": "abc1234", "accepted_issues": [140],
         "not_verified": list(entries)}
    r.update(extra)
    return r


def run(tmp_path, current, prior=None, args=None):
    (tmp_path / ".security-audit.json").write_text(json.dumps(current))
    if args is None:
        if prior is None:
            args = ["--no-prior"]
        else:
            (tmp_path / "prior.json").write_text(json.dumps(prior))
            args = ["--prior", str(tmp_path / "prior.json")]
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=tmp_path,
                          capture_output=True, text=True)


def ok(res):
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def fails(res, token):
    assert res.returncode == 1, (res.returncode, res.stdout)
    assert res.stdout == ""
    assert token in res.stderr, res.stderr


# ── output: keyed, not positional (#140) ────────────────────────────────

def test_valid_record_prints_the_fields_by_key(tmp_path):
    out = ok(run(tmp_path, record([nv("NV-001"), nv("NV-002", "resolved", resolution="probed")])))
    assert out == {"reviewed_commit": "abc1234", "accepted_issues": [140],
                   "not_verified_open": ["NV-001"]}


def test_an_empty_list_is_valid_and_reports_none_open(tmp_path):
    assert ok(run(tmp_path, record()))["not_verified_open"] == []


# ── the pre-#140 checks still hold ──────────────────────────────────────

@pytest.mark.parametrize("current,token", [
    (record(reviewed_commit="not-a-sha"), "reviewed_commit"),
    (record(accepted_issues=["140"]), "accepted_issues"),
    (record(accepted_issues=[True]), "accepted_issues"),
])
def test_existing_field_checks(tmp_path, current, token):
    fails(run(tmp_path, current), token)


def test_missing_record_fails(tmp_path):
    res = subprocess.run([sys.executable, str(SCRIPT), "--no-prior"], cwd=tmp_path,
                         capture_output=True, text=True)
    fails(res, "not found")


# ── not_verified shape ──────────────────────────────────────────────────

@pytest.mark.parametrize("entries,token", [
    ([nv("NV-1")], "is not NV-<3+ digits>"),
    ([nv("NV-001"), nv("NV-001")], "appears twice"),
    ([nv("NV-001", "closed")], "is not one of"),
    ([nv("NV-001", resolution="looked")], "is open but carries a resolution"),
    ([nv("NV-001", "resolved")], "has no resolution"),
    ([nv("NV-001", "not_applicable", resolution="  ")], "has no resolution"),
    ([nv("NV-001", owner="me")], "unknown key"),
    ([{"id": "NV-001", "summary": "s", "status": "open"}], "missing: first_seen"),
    ([nv("NV-001", summary="")], "summary must be a non-empty string"),
    ([nv("NV-001", refs="tsanetgit/Zendesk_App#1")], "refs must be a list"),
    (["NV-001"], "is not an object"),
], ids=["id-format", "duplicate-id", "bad-status", "open-with-resolution",
        "resolved-without-resolution", "blank-resolution", "unknown-key",
        "missing-key", "empty-summary", "refs-not-list", "not-an-object"])
def test_malformed_entries_fail(tmp_path, entries, token):
    fails(run(tmp_path, record(entries)), token)


def test_a_record_without_the_list_fails(tmp_path):
    r = record()
    del r["not_verified"]
    fails(run(tmp_path, r), "not_verified must be a list")


# ── lineage: an entry is never removed ──────────────────────────────────

def test_dropping_a_prior_entry_fails_and_names_it(tmp_path):
    prior = record([nv("NV-001"), nv("NV-002")])
    fails(run(tmp_path, record([nv("NV-001")]), prior), "NV-002")


def test_resolving_a_prior_entry_passes(tmp_path):
    prior = record([nv("NV-001"), nv("NV-002")])
    current = record([nv("NV-001"), nv("NV-002", "resolved", resolution="probed on BETA")])
    assert ok(run(tmp_path, current, prior))["not_verified_open"] == ["NV-001"]


def test_adding_an_entry_passes(tmp_path):
    prior = record([nv("NV-001")])
    assert ok(run(tmp_path, record([nv("NV-001"), nv("NV-002")]), prior))


def test_reopening_a_resolved_entry_passes(tmp_path):
    prior = record([nv("NV-001", "resolved", resolution="probed")])
    assert ok(run(tmp_path, record([nv("NV-001")]), prior))["not_verified_open"] == ["NV-001"]


@pytest.mark.parametrize("field,value", [
    ("summary", "a narrower claim that is easy to resolve"),
    ("first_seen", "review Y"),
])
def test_rewriting_a_carried_entry_fails(tmp_path, field, value):
    """An entry must not evaporate by edit: keep the id, swap the claim,
    then resolve the new one."""
    prior = record([nv("NV-001")])
    fails(run(tmp_path, record([nv("NV-001", **{field: value})]), prior),
          f"NV-001 {field} changed")


def test_refs_may_change_on_a_carried_entry(tmp_path):
    prior = record([nv("NV-001")])
    assert ok(run(tmp_path, record([nv("NV-001", refs=["tsanetgit/Zendesk_App#96"])]), prior))


def test_a_prior_from_before_the_list_has_nothing_to_drop(tmp_path):
    prior = record()
    del prior["not_verified"]
    assert ok(run(tmp_path, record([nv("NV-001")]), prior))


def test_a_missing_prior_file_fails_rather_than_skipping(tmp_path):
    """A typo in --prior must not quietly switch the lineage check off."""
    res = run(tmp_path, record(), args=["--prior", str(tmp_path / "typo.json")])
    fails(res, "not found")


def test_one_of_prior_or_no_prior_is_required(tmp_path):
    res = run(tmp_path, record(), args=[])
    assert res.returncode == 2 and res.stdout == ""


# ── the repository's own record ─────────────────────────────────────────

def test_the_committed_record_is_valid():
    res = subprocess.run([sys.executable, str(SCRIPT), "--no-prior"], cwd=REPO,
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
