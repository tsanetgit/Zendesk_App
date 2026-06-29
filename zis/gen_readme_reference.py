#!/usr/bin/env python3
"""Generate the machine-maintained reference block in zis/README.md from the bundle.

The bundle (tsanet_connect_bundle.json) is the source of truth. This script
renders a deterministic reference section (actions, job specs, flow states) and
splices it into README.md between the BEGIN/END markers below. Everything
outside the markers is hand-written prose and is never touched.

Usage:
  python3 zis/gen_readme_reference.py            # rewrite the block in place
  python3 zis/gen_readme_reference.py --check     # exit 1 if the block is stale (CI)

Run from the repo root or from zis/; paths are resolved relative to this file.
"""
import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUNDLE = HERE / "tsanet_connect_bundle.json"
README = HERE / "README.md"

BEGIN = "<!-- BEGIN GENERATED: bundle reference (do not edit by hand; run zis/gen_readme_reference.py) -->"
END = "<!-- END GENERATED: bundle reference -->"

MUSTACHE = re.compile(r"\{\{\s*\$\.([A-Za-z0-9_.]+)\s*\}\}")


def _short(name: str) -> str:
    """zis:tsanet_connect:flow:flow_x -> flow_x ; leave plain names as-is."""
    return name.split(":")[-1] if name and ":" in name else name


def _loc(definition: dict) -> str:
    """Human-readable endpoint: prefer url, else path; collapse mustache vars."""
    raw = definition.get("url") or definition.get("path") or ""
    return MUSTACHE.sub(lambda m: "{" + m.group(1) + "}", raw)


def render(bundle: dict) -> str:
    res = bundle["resources"]
    actions, jobspecs, flows = {}, {}, {}
    for k, v in res.items():
        t = v.get("type")
        if t == "ZIS::Action::Http":
            actions[k] = v
        elif t == "ZIS::JobSpec":
            jobspecs[k] = v
        elif t == "ZIS::Flow":
            flows[k] = v

    lines = []
    lines.append("> Generated from `tsanet_connect_bundle.json` by `zis/gen_readme_reference.py`.")
    lines.append("> Do not edit between the markers; run the script to refresh.")
    lines.append("")
    lines.append(f"Bundle `{bundle.get('name')}` · template `{bundle.get('zis_template_version')}` · "
                 f"{len(actions)} actions, {len(flows)} flows, {len(jobspecs)} job specs.")
    lines.append("")

    lines.append("### Job specs (event → flow)")
    lines.append("")
    lines.append("| Job spec | event_source | event_type | Flow |")
    lines.append("|---|---|---|---|")
    for k in sorted(jobspecs):
        p = jobspecs[k]["properties"]
        lines.append(f"| `{k}` | `{p.get('event_source')}` | `{p.get('event_type')}` | `{_short(p.get('flow_name'))}` |")
    lines.append("")

    lines.append("### Actions")
    lines.append("")
    lines.append("| Action | Connection | Method | Endpoint |")
    lines.append("|---|---|---|---|")
    for k in sorted(actions):
        d = actions[k]["properties"]["definition"]
        lines.append(f"| `{k}` | `{d.get('connectionName')}` | {d.get('method')} | `{_loc(d)}` |")
    lines.append("")

    lines.append("### Flows (states)")
    lines.append("")
    for k in sorted(flows):
        fd = flows[k]["properties"]["definition"]
        states = fd.get("States", {})
        lines.append(f"- **`{k}`** — StartAt `{fd.get('StartAt')}`")
        for sn in sorted(states):
            st = states[sn]
            stype = st.get("Type")
            extra = ""
            if stype == "Action":
                extra = f" → `{_short(st.get('ActionName'))}`"
            lines.append(f"  - `{sn}` ({stype}){extra}")
    return "\n".join(lines)


def build_block(bundle: dict) -> str:
    return f"{BEGIN}\n{render(bundle)}\n{END}"


def splice(readme_text: str, block: str) -> str:
    pat = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.DOTALL)
    if pat.search(readme_text):
        return pat.sub(lambda _: block, readme_text)
    # No markers yet: append as a new appendix section.
    sep = "" if readme_text.endswith("\n") else "\n"
    return f"{readme_text}{sep}\n## Reference (generated)\n\n{block}\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="exit non-zero if README.md is out of sync with the bundle")
    args = ap.parse_args()

    bundle = json.loads(BUNDLE.read_text())
    current = README.read_text()
    block = build_block(bundle)
    updated = splice(current, block)

    if args.check:
        if updated != current:
            sys.stderr.write(
                "zis/README.md reference block is stale.\n"
                "Run: python3 zis/gen_readme_reference.py  (then commit zis/README.md)\n"
            )
            return 1
        print("zis/README.md reference block is up to date.")
        return 0

    if updated != current:
        README.write_text(updated)
        print(f"Updated reference block in {README}")
    else:
        print("No change; reference block already current.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
