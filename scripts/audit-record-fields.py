#!/usr/bin/env python3
"""Validate .security-audit.json and print the fields the release gate needs.

Called by the `security-status` job in .github/workflows/release.yml. Lives in a
file rather than a heredoc inside the workflow for two reasons: a heredoc body
must sit at the block scalar's base indentation or it silently terminates the
YAML block (which is exactly how this check first shipped broken), and a script
can be unit-tested on its own.

Prints two lines on success:

    <reviewed_commit>
    <space-separated accepted issue numbers, possibly empty>

Exits nonzero with a reason on stderr if the record is missing, unparseable, or
fails schema validation. The caller treats ANY nonzero exit as
"coverage indeterminate", never as "no findings".

That distinction is the whole point. A partial read is the dangerous failure: a
missing key becomes an empty string, an empty string compared numerically is a
shell error, and a shell error reads as false — which is assurance manufactured
out of a parse failure. Same class as tsanetgit/Zendesk_App#108.

Exit codes:
  0  valid; fields printed on stdout
  1  missing, unreadable, or schema-invalid (reason on stderr)
"""
from __future__ import annotations

import json
import re
import sys

PATH = ".security-audit.json"
SHA_RE = re.compile(r"[0-9a-f]{7,40}")


def main() -> int:
    try:
        with open(PATH, encoding="utf-8") as f:
            record = json.load(f)
    except FileNotFoundError:
        print(f"{PATH} not found", file=sys.stderr)
        return 1
    except (OSError, json.JSONDecodeError) as e:
        print(f"{PATH} is unreadable: {e}", file=sys.stderr)
        return 1

    if not isinstance(record, dict):
        print(f"{PATH} is not a JSON object", file=sys.stderr)
        return 1

    reviewed = str(record.get("reviewed_commit", ""))
    if not SHA_RE.fullmatch(reviewed):
        print(f"reviewed_commit is not a hex sha: {reviewed!r}", file=sys.stderr)
        return 1

    # Absent is legitimate and means "nothing accepted", which is the strict
    # reading. It must never be confused with "could not read the field".
    accepted = record.get("accepted_issues", [])
    if not isinstance(accepted, list) or not all(
        isinstance(x, int) and not isinstance(x, bool) for x in accepted
    ):
        print("accepted_issues must be a list of integers", file=sys.stderr)
        return 1

    print(reviewed)
    print(" ".join(str(x) for x in accepted))
    return 0


if __name__ == "__main__":
    sys.exit(main())
