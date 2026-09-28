#!/usr/bin/env python3
"""Validate .security-audit.json and print the fields the release gate needs.

Called by the `security-status` job in .github/workflows/release.yml, and by
tests.yml on every change to the record. Lives in a file rather than a heredoc
inside the workflow for two reasons: a heredoc body must sit at the block
scalar's base indentation or it silently terminates the YAML block (which is
exactly how this check first shipped broken), and a script can be unit-tested
on its own.

Usage:

    audit-record-fields.py --prior PRIOR_RECORD   # also checks the lineage
    audit-record-fields.py --no-prior             # when no prior record exists

One of the two is required. A missing prior file is an error, never "no
prior": a typo in the path must not quietly switch the lineage check off.

Prints one JSON object on success, keyed rather than positional (#140):

    {"reviewed_commit": "<sha>", "accepted_issues": [<int>, ...],
     "not_verified_open": ["NV-001", ...]}

Exits nonzero with a reason on stderr if the record is missing, unparseable,
fails schema validation, or drops a `not_verified` entry the prior record
held. The caller treats ANY nonzero exit as "coverage indeterminate", never as
"no findings".

That distinction is the whole point. A partial read is the dangerous failure: a
missing key becomes an empty string, an empty string compared numerically is a
shell error, and a shell error reads as false — which is assurance manufactured
out of a parse failure. Same class as tsanetgit/Zendesk_App#108.

`not_verified` (#140) is the review's list of claims it could not check. Each
entry is {id, summary, first_seen, status, refs?, resolution?}: `id` is
NV-<3+ digits> and never reused, `status` is open, resolved or not_applicable,
and `resolution` is required exactly when the status is not open. An entry is
never removed. A later review may change its status, but every id the prior
record held must still be present, so a known unknown cannot evaporate the way
the v1.0.50 record's did, as prose in `notes`.

Exit codes:
  0  valid; fields printed on stdout
  1  missing, unreadable, schema-invalid, or an entry dropped (reason on stderr)
  2  bad arguments (argparse)
"""
from __future__ import annotations

import argparse
import json
import re
import sys

PATH = ".security-audit.json"
SHA_RE = re.compile(r"[0-9a-f]{7,40}")
NV_ID_RE = re.compile(r"NV-\d{3,}")
NV_STATUSES = ("open", "resolved", "not_applicable")
NV_REQUIRED = ("id", "summary", "first_seen", "status")
NV_KEYS = frozenset(NV_REQUIRED + ("refs", "resolution"))


class RecordError(Exception):
    pass


def _load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
    except FileNotFoundError:
        raise RecordError(f"{path} not found")
    except (OSError, json.JSONDecodeError) as e:
        raise RecordError(f"{path} is unreadable: {e}")
    if not isinstance(record, dict):
        raise RecordError(f"{path} is not a JSON object")
    return record


def _nonempty_str(v) -> bool:
    return isinstance(v, str) and v.strip() != ""


def _not_verified(record: dict) -> list:
    """The record's entries, validated. Raises RecordError on any defect."""
    entries = record.get("not_verified")
    if not isinstance(entries, list):
        raise RecordError("not_verified must be a list (empty when nothing is unverified)")
    seen = set()
    for n, e in enumerate(entries):
        where = f"not_verified[{n}]"
        if not isinstance(e, dict):
            raise RecordError(f"{where} is not an object")
        unknown = sorted(set(e) - NV_KEYS)
        if unknown:
            raise RecordError(f"{where} has unknown key(s): {', '.join(unknown)}")
        missing = [k for k in NV_REQUIRED if k not in e]
        if missing:
            raise RecordError(f"{where} is missing: {', '.join(missing)}")
        nv_id = e["id"]
        if not isinstance(nv_id, str) or not NV_ID_RE.fullmatch(nv_id):
            raise RecordError(f"{where} id {nv_id!r} is not NV-<3+ digits>")
        if nv_id in seen:
            raise RecordError(f"not_verified id {nv_id} appears twice")
        seen.add(nv_id)
        for k in ("summary", "first_seen"):
            if not _nonempty_str(e[k]):
                raise RecordError(f"{nv_id} {k} must be a non-empty string")
        if e["status"] not in NV_STATUSES:
            raise RecordError(f"{nv_id} status {e['status']!r} is not one of "
                              f"{', '.join(NV_STATUSES)}")
        # Both directions: an open entry with a resolution reads as settled
        # when it is not, and a closed one without one records no reason.
        if e["status"] == "open":
            if "resolution" in e:
                raise RecordError(f"{nv_id} is open but carries a resolution")
        elif not _nonempty_str(e.get("resolution")):
            raise RecordError(f"{nv_id} is {e['status']} but has no resolution")
        refs = e.get("refs", [])
        if not isinstance(refs, list) or not all(_nonempty_str(r) for r in refs):
            raise RecordError(f"{nv_id} refs must be a list of non-empty strings")
    return entries


def _prior_ids(prior: dict) -> set:
    """Ids the prior record held. A record from before #140 has none."""
    entries = prior.get("not_verified", [])
    if not isinstance(entries, list):
        raise RecordError("prior record's not_verified is not a list")
    ids = set()
    for e in entries:
        if not isinstance(e, dict) or not isinstance(e.get("id"), str):
            raise RecordError("prior record has a not_verified entry without an id")
        ids.add(e["id"])
    return ids


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    which = ap.add_mutually_exclusive_group(required=True)
    which.add_argument("--prior", metavar="PRIOR_RECORD",
                       help="the previous record, to check no entry was dropped")
    which.add_argument("--no-prior", action="store_true",
                       help="no previous record exists; skip the lineage check")
    args = ap.parse_args(argv)

    try:
        record = _load(PATH)

        reviewed = str(record.get("reviewed_commit", ""))
        if not SHA_RE.fullmatch(reviewed):
            raise RecordError(f"reviewed_commit is not a hex sha: {reviewed!r}")

        # Absent is legitimate and means "nothing accepted", which is the strict
        # reading. It must never be confused with "could not read the field".
        accepted = record.get("accepted_issues", [])
        if not isinstance(accepted, list) or not all(
            isinstance(x, int) and not isinstance(x, bool) for x in accepted
        ):
            raise RecordError("accepted_issues must be a list of integers")

        entries = _not_verified(record)
        if args.prior:
            dropped = sorted(_prior_ids(_load(args.prior))
                             - {e["id"] for e in entries})
            if dropped:
                raise RecordError(
                    f"not_verified entr{'y' if len(dropped) == 1 else 'ies'} "
                    f"{', '.join(dropped)} in the prior record "
                    f"{'is' if len(dropped) == 1 else 'are'} missing here. Carry "
                    f"each forward, or set its status to resolved or "
                    f"not_applicable with a resolution; an entry is never removed")
    except RecordError as e:
        print(str(e), file=sys.stderr)
        return 1

    print(json.dumps({
        "reviewed_commit": reviewed,
        "accepted_issues": accepted,
        "not_verified_open": [e["id"] for e in entries if e["status"] == "open"],
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
