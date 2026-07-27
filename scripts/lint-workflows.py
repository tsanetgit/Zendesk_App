#!/usr/bin/env python3
"""Parse every GitHub Actions workflow file and fail if one is invalid.

Why this exists
---------------
On 2026-07-27 a change to release.yml embedded a heredoc whose body sat at
indent 0 inside a `run: |` block scalar indented 10 spaces. In YAML that
terminates the block, so the file stopped parsing and the release workflow was
dead. GitHub reported it at push time; the local check that was supposed to
catch it ran `bash -n` on a *dedented extraction* of the run block, which
passed happily because it was not testing the file.

This parses the files themselves. It is deliberately dumb: it does not validate
Actions semantics, only that each workflow is loadable YAML with the two keys
that make it a workflow at all. That is enough to catch the entire class of
"the file no longer parses" and it needs no third-party action.

Usage:
  scripts/lint-workflows.py [--dir .github/workflows]

Exit codes:
  0  every workflow parses
  1  at least one failed (details on stdout)
  2  the linter could not run (no directory, or PyYAML missing)
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

try:
    import yaml
except ImportError:
    print("::error::PyYAML is not available, so workflows were NOT checked. "
          "That is an indeterminate result, not a pass.")
    sys.exit(2)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", default=".github/workflows")
    args = ap.parse_args()

    if not os.path.isdir(args.dir):
        print(f"::error::{args.dir} does not exist; nothing was checked.")
        return 2

    paths = sorted(
        p for ext in ("yml", "yaml")
        for p in glob.glob(os.path.join(args.dir, f"*.{ext}"))
    )
    # An empty run passing is the same failure as a stale hardcoded list: it
    # reports success having inspected nothing.
    if not paths:
        print(f"::error::no workflow files found under {args.dir}; "
              "the linter had nothing to inspect.")
        return 2

    failures = 0
    for path in paths:
        try:
            with open(path, encoding="utf-8") as f:
                doc = yaml.safe_load(f)
        except yaml.YAMLError as e:
            mark = getattr(e, "problem_mark", None)
            where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
            print(f"::error file={path}::does not parse as YAML{where}: "
                  f"{getattr(e, 'problem', e)}")
            failures += 1
            continue
        except OSError as e:
            print(f"::error file={path}::unreadable: {e}")
            failures += 1
            continue

        if not isinstance(doc, dict):
            print(f"::error file={path}::parsed, but the top level is "
                  f"{type(doc).__name__}, not a mapping.")
            failures += 1
            continue

        # PyYAML resolves a bare `on:` key to the boolean True, so accept both.
        has_on = ("on" in doc) or (True in doc)
        if not has_on or "jobs" not in doc:
            missing = [k for k, present in (("on", has_on), ("jobs", "jobs" in doc))
                       if not present]
            print(f"::error file={path}::parses, but is missing "
                  f"{', '.join(missing)} — not a usable workflow.")
            failures += 1
            continue

        print(f"  ok  {path} ({len(doc['jobs'])} job(s))")

    print(f"\n{len(paths) - failures} of {len(paths)} workflow file(s) valid")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
