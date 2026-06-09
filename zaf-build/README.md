# TSANet Connect — ZAF Sidebar App (Source Bundle)

A Zendesk Apps Framework (ZAF) sidebar app that embeds TSANet Connect collaboration directly into the Zendesk ticket view.

**Current version:** v1.0.29
**Distribution:** Private ZAF app, ZIP upload via Zendesk Admin Center.

---

## Two ways to use this bundle

### 1. Install the pre-built app (recommended)

Most members should not build from source. Install the pre-built ZIP that TSANet publishes on each release:

> Admin Center → Apps and integrations → Zendesk Support apps → Upload private app
> Filename: `tsanet-connect-v1.0.29.zip`

Configure the eight settings (TSANet credentials + your five Zendesk custom field IDs) and you're done. No build tools, no Node, no command line. Five minutes total.

### 2. Customize from this source bundle

If you want to extend the app with company-specific logic (extra panels, additional integrations, branding changes), this source bundle is the starting point.

---

## What's in this bundle

```
zaf-build/
├── manifest.json              ← App metadata, parameters, location
├── README.md                  ← This file
├── assets/
│   ├── index.html             ← Sidebar HTML shell + ZAF SDK CDN script
│   ├── main.js                ← All app logic (single bundled file)
│   ├── background.html        ← Background page (5-min poller, SLA breach detection)
│   └── logo.png               ← App icon (128×128 transparent PNG)
└── translations/
    └── en.json                ← Localization strings (required even if minimal)
```

**About the structure:** This bundle ships a *flat, pre-bundled* JavaScript file (`assets/main.js`, ~38 KB, ~840 lines) rather than a multi-file source tree. There is no Vite build step required — what you see is what runs in Zendesk. To customize, edit `main.js` directly and rezip.

A multi-file Vite-based source tree (with `src/api/`, `src/components/`, `src/utils/`) existed in earlier versions and is preserved at `tsanet-connect-zendesk-zaf/zaf-app/` for reference, but it has not been kept in sync with the bundled `assets/main.js` — it represents v1.0.20-era code. A proper re-modularization is planned for the v1.1 milestone. **Do not start customization from the `zaf-app/` tree** — start from this bundle (`zaf-build/`).

---

## Customizing and rebuilding

```bash
# 1. Unzip this bundle to a working directory
unzip tsanet-connect-zaf-app-source.zip -d my-tsanet-app

# 2. Edit assets/main.js (and optionally index.html, background.html, manifest.json)
# Search for clearly-marked sections like:
#   // ── TSANet Auth ─────────
#   // ── Modal helpers ───────
#   // ── Notes ───────────────
#   // ── Background sync ─────

# 3. Bump the version in manifest.json
#    "version": "1.0.30"

# 4. Repackage the bundle
cd my-tsanet-app
zip -r ../my-tsanet-app-v1.0.30.zip zaf-build -x "*.DS_Store"

# 5. Upload via Admin Center → Apps and integrations → Zendesk Support apps → Update
```

That's it. No `npm install`, no `vite build`, no scripts.

---

## App Settings (filled in on install)

All defaults are intentionally empty. The Zendesk install dialog will block install until required fields are filled.

| Setting | Required | Description |
|---|---|---|
| `tsanet_username` | ✅ | TSANet API user email (e.g. `api@yourcompany.com`) |
| `tsanet_password` | ✅ | TSANet API password |
| `tsanet_env` | ✅ | `BETA` or `PRODUCTION` (default: `BETA`) |
| `field_id_token` | ✅ | Numeric Field ID of your TSANet Token custom field |
| `field_id_tokens_multi` | — | Numeric Field ID of your TSANet Tokens Multi custom field (optional) |
| `field_id_status` | ✅ | Numeric Field ID of your TSANet Status custom field |
| `field_id_partner` | ✅ | Numeric Field ID of your TSANet Partner custom field |
| `field_id_respond_by` | ✅ | Numeric Field ID of your TSANet Respond By custom field |

To find your Field IDs: Admin Center → Objects and rules → Tickets → Fields → click the field → look at the URL (e.g. `…/fields/48849323029652`).

---

## Architecture Notes

- **ZAF SDK** loads from Zendesk's CDN at runtime — no local npm install needed.
- **TSANet auth** uses JWT Bearer tokens cached for ~50 minutes; refreshed automatically.
- **Zendesk auth** is automatic — ZAF inherits the agent's session via `client.request()`.
- **No external server** required for the sidebar app itself. All API calls proxy through Zendesk.
- **Background page** (`assets/background.html`) runs while any agent has Zendesk open — polls TSANet every 5 minutes for new inbound cases and SLA breaches.
- **Server-side complement:** a GitHub Actions workflow (separate from this bundle, see ZIS Quick Start) refreshes the ZIS bearer token and detects SLA breaches when no agent is online. Both pieces are needed for a complete deployment.
- **Stateless** — all state lives in Zendesk ticket fields and the TSANet API. The app holds no database.

---

## Known constraints worth respecting if you customize

These are gotchas that took weeks to discover. Search `main.js` for the named functions to see how they're handled:

- **`prompt()` and `confirm()` are silently blocked** in cross-origin ZAF iframes. Use the `showPrompt()`, `showPrompt2()`, and `showConfirm()` custom modal helpers instead.
- **`engineerEmail` is a required field on Accept** (TSANet API undocumented). Use `settings.tsanet_username` — the agent's Zendesk email won't satisfy domain validation.
- **TSANet returns HTML in note descriptions.** Use `stripHtml()` before display.
- **Zendesk date fields require `YYYY-MM-DD`**, not ISO datetime. Truncate with `.substring(0, 10)`.
- **Notes mirroring uses a `tsanet-note-id:{id}` marker** embedded in Zendesk comment bodies for deduplication. Don't break the marker format.
- **SLA countdown is acknowledgment-only** — gate display on `responded === false`. Once Accepted/Rejected/Info-Requested, TSANet stops tracking the deadline.
- **Close button must be hidden on inbound cases** — TSANet API restricts closure to the submitting party.

For the full list and detailed explanations, see the published `SKILL_TSANet_Connect.md` skill file in the docs repo. If you use Claude Code or another agentic assistant for customization, drop that skill file into `~/.claude/skills/tsanet-connect/SKILL.md` and your assistant will know all of these gotchas before writing a line of code.

---

## License & Contact

Distributed by TSANet for use by member companies.

**Issues, questions, contributions:** membership@tsanet.org
