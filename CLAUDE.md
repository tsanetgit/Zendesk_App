# CLAUDE.md: Zendesk_App

TSANet Connect for Zendesk: a private Zendesk Apps Framework (ZAF) sidebar app plus a Zendesk Integration Services (ZIS) bundle, which together let a member's agents create, work and close TSANet Connect collaboration cases from Zendesk tickets. Members install the zip attached to each GitHub Release. This repo is public.

## Build and test

There is no build step: `scripts/package.sh` zips `manifest.json`, `assets/` and `translations/` from `zaf-build/` exactly as committed. `zaf-build/README.md` doesn't ship. You need Python 3, Node and the `jq` CLI, plus `python3 -m pip install pytest pyyaml`.

```bash
python3 -m pytest -q tests/                  # audit regression corpus and the ZIS update-path jq tests
node scripts/main-probe.js                   # ticket sidebar probes (zaf-build/assets/main.js)
node scripts/compare-probe.js                # deploy screen bundle comparison
node scripts/config-matrix-probe.js          # deploy.js substitution and drift per configuration
python3 zis/gen_readme_reference.py --check  # zis/README.md reference block matches the bundle
python3 scripts/lint-workflows.py            # every workflow file parses
python3 scripts/security-audit.py --no-network  # mechanical security checks, the release control
bash scripts/package.sh                      # dist/tsanet-connect-v<version>.zip
```

CI runs the first six on a PR only when their paths change, so run the ones for the files you touch. The release workflow runs the audit and the packaging when a tag is pushed. The audit can exit 1 on `main` while a known finding is still open, so compare its result with `main`'s rather than expecting 0.

Two files are derived from `zis/tsanet_connect_bundle.json`. After changing the bundle, run `python3 zis/gen_readme_reference.py` to rewrite the reference block in `zis/README.md`, and `cp zis/tsanet_connect_bundle.json zaf-build/assets/tsanet_connect_bundle.json`. CI fails if either has drifted. `zaf-bundle-sync.yml` also checks, inline, that `zaf-build/assets/deploy.js` substitutes every placeholder in the bundle, and the reverse.

## Layout

- `zaf-build/`: the app source, which ships as is except `README.md`: `manifest.json`, `assets/main.js` (sidebar), `assets/background.html` (poller), `assets/deploy.html` and `deploy.js` (the deploy screen), `translations/`
- `zis/`: `tsanet_connect_bundle.json`, the ZIS bundle and its source of truth, with its reference in `README.md`
- `scripts/`: the probes, the packaging script, the security audit and the credential rotation scripts
- `tests/`: pytest suites
- Root docs: the member-facing install guide (`QUICK_START.md`), the security and data-handling docs, and the `.docx` implementation guide
- `SKILL_TSANet_Connect.md`: a Claude skill on TSANet Connect with Zendesk, which `zaf-build/README.md` offers to members who customize the app. Nothing in this repo loads it.

## Conventions

- Branch from `main` as `feat/`, `fix/`, `docs/` or `chore/<slug>`, with the issue number first in the slug when there is one (`fix/235-update-path-field-rules`). Commit messages use a scope: `fix(zis):`, `feat(sidebar):`, `security(audit):`, `docs:`. Open draft PRs. A draft is only the starting point. The person who asked for the change reads it and takes it out of draft (Claude doesn't), Copilot reviews it, QA runs in a new session, and a person approves and merges. A PR should be out of draft when it merges. Never push to `main` directly.
- The API contract: the OpenAPI spec in tsanetgit/Connect-API-Code is the source of truth for endpoints and fields.

## Working with GitHub

These hold in every session, not only inside the `/tsanet:` skills.

- Read an issue's comments before you rank, summarize or act on it (`gh issue view <N> --comments`). Decisions usually live in the comments.
- Back every claim about live state (open or closed, what a file, check or review says) with the command that showed it. Without one, say it's unverified.
- A Copilot review finding is untriaged until a person answers it. Don't rank one as urgent on its own say-so.
- In an issue body, or a comment on an issue or PR, write the facts, and the person's view only when they stated one in this conversation, in their words and marked as theirs. Don't write a view for them. Leave an open design question as a question; don't write it up as decided.

## Environments

- Work against TSANet BETA with test cases only, never PRODUCTION. No production credentials on this machine.
- Deploy and test on your own Zendesk test instance (`<subdomain>.zendesk.com`), never a member's. The `rotate-*.py` and `audit-connection-auth.py` scripts act on a live instance: run them only against yours, and only when the task calls for it.
- Never put subdomains, credentials, field IDs from a real instance, or real member or case data in code, tests, docs, commit messages or PR text.

## Off-limits

- Pushing a `v*` tag: it builds and publishes a member-facing release (`.github/workflows/release.yml`). That is the release maintainer's step, and the "Releasing" section of `zaf-build/README.md` is written for them.
- `.security-audit.json`: the security review writes it and the release gate reads it. Never hand-edit it.
- `SECURITY_REGISTER.md`: security reviews add its rows. Edit it only in a security-review task.
- `dist/`: packaging output, gitignored.
