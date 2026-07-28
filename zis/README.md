# TSANet Connect — ZIS Flow Bundle

The ZIS flow bundle for **event-driven inbound** (ping-then-pull): TSANet pings a ZIS inbound webhook, the flow pulls the full collaboration from the TSANet API, and a Zendesk ticket is created (new case) or updated (existing case). No scheduled polling, no token-refresh jobs.

This directory is the **source of truth** for the bundle. The deployed copy in any Zendesk instance should match this file (with the per-instance substitutions below).

```
zis/
├── tsanet_connect_bundle.json   ← the bundle (flows, actions, job spec)
└── README.md                    ← this file
```

## What the flow does

```
TSANet webhook ping (eventType + requestToken)
  → ZIS inbound webhook (Basic auth)            … via callbackAuth (issue #2, in API v3.1.0)
  → jobspec_handle_ping → flow_handle_ping
      GetCollaboration   pull full case from TSANet API   (OAuth connection "tsanet_oauth")
      SearchTicket       find ticket by TSANet Token field (connection "zendesk")
      CheckTicketExists  branch on whether the ticket already exists
      ├─ exists   → TransformForUpdate → UpdateTicket
      │              jq: status → lowercase option value, respondBy → YYYY-MM-DD
      └─ no match → GuardCreate                                   (idempotency guard, issue #42)
                     ├─ eventType collaboration-request.created → CreateTicket  new ticket w/ token/status/partner
                     └─ else (note.created, any V2 type)        → NoOp   only the creation event may create
```

## Prerequisites

1. The ZIS integration `tsanet_connect` exists and the **OAuth client-credentials connection** `tsanet_oauth` is configured — see [ZIS_Quick_Start.md](../ZIS_Quick_Start.md) Steps 1–4.
2. An **OAuth connection named `zendesk`** for the Zendesk-side actions (path-only actions do **not** auto-authenticate). Earlier revisions used a basic-auth connection holding an API token; Zendesk is retiring API tokens for the Ticketing API (creation blocked for new accounts **2026-07-28** and for all accounts **2026-10-27**; all existing tokens stop working **2027-04-30**), so the connection now stores an auto-renewing OAuth credential. Full background: [Zendesk_API_Credential_Decision.md](../Zendesk_API_Credential_Decision.md). Three steps (validated 2026-07-07):

   **2a. Use the integration's confidential OAuth client** created in
   [ZIS_Quick_Start.md](../ZIS_Quick_Start.md) **Step 1** (Admin Center → OAuth
   clients, **Client kind: Confidential**, created as the dedicated service user —
   client_credentials tokens act as the user associated with the client). The same
   client that mints your setup tokens backs this connection; you need its
   identifier (`tsanet_zendesk`) and secret. Reminder: the full secret is shown
   **only once** at creation, and the client must be confidential **at creation** —
   the grant rejects public clients (`unauthorized_client`), and changing `kind`
   later regenerates the secret while only ever displaying it truncated (delete and
   recreate the client if that happens).

   **2b. Register a ZIS OAuth client** pointing at your **own** instance's token endpoint (uses the ZIS bearer from Quick Start Step 4):
   ```bash
   curl -s -X POST "https://YOURSUBDOMAIN.zendesk.com/api/services/zis/connections/oauth/clients/tsanet_connect" \
     -H "Authorization: Bearer ZIS_OAUTH_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{
       "name": "zendesk_self",
       "grant_type": "client_credentials",
       "client_id": "tsanet_zendesk",
       "client_secret": "YOUR_CLIENT_SECRET",
       "token_url": "https://YOURSUBDOMAIN.zendesk.com/oauth/tokens",
       "default_scopes": "read tickets:write"
     }'
   ```
   `read tickets:write` is the **minimal scope** for the bundle's seven Zendesk-side calls: `tickets:read` alone is not enough — `/api/v2/search.json` returns 403 under it (`SearchTicket` breaks), and there is no `search:read` scope. Ticket create/update need `tickets:write`.

   **2c. Create the connection named `zendesk`** (same start + verification dance as Quick Start Step 4b):
   ```bash
   curl -s -X POST "https://YOURSUBDOMAIN.zendesk.com/api/services/zis/connections/oauth/start/tsanet_connect" \
     -H "Authorization: Bearer ZIS_OAUTH_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"oauth_client_name": "zendesk_self", "name": "zendesk"}'
   ```
   GET the returned `redirect_url` (with the same ZIS bearer) to complete creation, then verify the connection holds an `access_token` with `token_expiry` about 30 minutes out (Zendesk applies a 30-minute expiry to tokens from clients created on or after 2026-04-30). ZIS mints and renews the short-lived tokens itself — nothing scheduled, exactly like the `tsanet_oauth` connection. Renewal verified live: an expired token was re-minted automatically on next access, with `token_expiry` advancing a fresh 30 minutes.

   **Migrating an existing install:** connection names are unique **across** types, so delete the old basic-auth connection named `zendesk` first, then run 2c to create the OAuth one under the same name. The bundle's seven Zendesk-side actions reference the connection by name only — **zero bundle changes**. There is a brief gap between delete and create; do it in a quiet window.

## Per-instance substitutions

**You do not edit this JSON.** The TSANet Connect app substitutes every value below
from its own app settings at deploy time, then refuses to upload if any placeholder
survives. The table is a reference for *what* varies per instance — which is what
you are being asked for on the app's settings screen, and what to check when a flow
misbehaves.

| What | Where | Note |
|---|---|---|
| Custom field IDs | `action_create_ticket`, `action_search_ticket`, `action_update_ticket` | The three numeric IDs (TSANet Token / Status / Partner, plus Respond By in the update action), taken from **your** instance's fields |
| Field-driven field IDs — **optional** | `flow_field_action` `GuardField`, `action_zd_finish_status` / `_silent` / `_fail`, and the `Extract` jq | The two field-driven fields: **TSANet Action** dropdown (`1234567891`) and **TSANet Action Text** (`1234567895`). Leave **both** blank and the resources holding these placeholders are omitted from the upload entirely — see *Field-driven case actions* below |
| API host | **all five** TSANet API actions — `action_get_collaboration`, `action_ts_accept`, `action_ts_reject`, `action_ts_info`, `action_ts_note` | File ships with Production (`connect2.tsanet.org`); Beta is `connect2.tsanet.net`. The host appears in every action that calls the TSANet API, not just `action_get_collaboration` — all five move together or the lifecycle actions hit the wrong environment |
| `engineerEmail` — **optional** | `action_ts_accept` | Your TSANet API user email, in place of `YOUR_TSANET_API_EMAIL`. It **must** be on your member-registered domain — TSANet's Accept endpoint rejects emails from any other domain. Needed only with field actions on: `action_ts_accept` is the only place it appears, and that action ships only when they are enabled. See *Field-driven case actions* below |
| OAuth connection name | **all five** TSANet API actions (the same five as API host) | File ships with `tsanet_oauth`. If your instance named its OAuth connection differently (e.g. `tsanet_beta_oauth`), that name has to reach **all five** actions, or every TSANet call fails auth against a nonexistent connection. Symptom: ingest accepts (HTTP 200) but the flow's `action_ts_*` silently no-op via their `Catch`. Verify the live name with `GET /api/services/zis/connections/{integration}?name=<name>` |

Connection name `zendesk` (Zendesk-side actions) matches the Quick Start. The TSANet OAuth connection name is per-instance — see the row above.

> Validated end-to-end on Beta (`connect2.tsanet.net`): authenticated webhook deliveries return 200 and the flow creates Zendesk tickets. The per-instance set is field IDs, host, `engineerEmail`, **and the OAuth connection name** — nothing else is environment-specific.

## Deploy

Deployment is two steps: the **TSANet Connect app** uploads the bundle and installs
its job specs, then one curl creates the inbound webhook.

### 1. Upload the bundle and install job specs — in the app

**Requires TSANet Connect app v1.0.52 or later.** Update the app first if the screen
below is missing.

1. In Zendesk Support, open **TSANet Connect** from the left nav bar.
2. Check the **Pre-flight** results. All three must pass before the button enables.
3. Click **Deploy bundle**.

The app substitutes every per-instance value from its own app settings, uploads the
bundle, installs each job spec, and then **reads the registry back** to confirm what
is actually installed. It reports per-step results and offers **Retry all** if any
step fails.

Why the app rather than curl: this endpoint **rejects OAuth** (401 `Authorization
failed due to OAuth being disabled for this API request`, re-verified 2026-07-27).
It accepts only an API token or an authenticated admin session. Zendesk blocks API
token creation for accounts created on or after **2026-07-28**, blocks new tokens for
everyone after **2026-10-27**, and deactivates all tokens on **2027-04-30**. The app
runs on the admin's own session, so there is no credential to create or maintain.

Notes:

- **Deploying replaces the installed bundle**, and an upload orphans the currently
  installed job specs. The app re-installs them immediately, but the integration is
  briefly inactive in between. Do not close the tab mid-run.
- **You must be a Zendesk administrator.** The screen is visible to any agent,
  because this project has not found an admin-only ZAF location, so the app's own
  role check is a convenience gate rather than a security boundary. The boundary is
  server-side: Zendesk documents the ZIS registry endpoints as
  [Allowed for: Admins](https://developer.zendesk.com/api-reference/integration-services/registry/bundles/).
  That is vendor documentation; an agent-role session has not been tested against
  these endpoints by TSANet.
- Job specs left installed from an **older bundle generation** still intercept
  events. The app surfaces these as a warning; uninstall them with
  `DELETE /api/services/zis/registry/job_specs/install?job_spec_name=...`.

### 2. Create the inbound webhook — curl

Not done by the app. Returns the ingest path, Basic credentials, and **`uuid`** —
keep all three. The `uuid` is REQUIRED for credential rotation and there is no list
API to recover a lost one (see `ZIS_Rotation_Runbook.md`).

```bash
curl -X POST "https://YOURSUBDOMAIN.zendesk.com/api/services/zis/inbound_webhooks/generic/tsanet_connect" \
  -H "Authorization: Bearer ZIS_OAUTH_TOKEN" -H "Content-Type: application/json" \
  -d '{"source_system":"tsanet","event_type":"collaboration_event"}'
```

The webhook subscription on the TSANet side uses the `callbackAuth` capability (issue #2), delivered in API **v3.1.0**: register with `callbackUrl` = the ingest URL and `callbackAuth` of type `BASIC` carrying the ingest credentials. TSANet attaches them to every delivery POST alongside the existing `X-Hub-Signature-256` HMAC, and the ZIS ingest accepts the authenticated request (validated on Beta: deliveries return 200 and create tickets). The pipeline can also be exercised without a live subscription by POSTing a `WebhookPayload`-shaped body (`eventType`, `requestToken`, `timestamp`) to the ingest URL with the Basic credentials.

## Field-driven case actions (no ZAF app required)

The bundle also includes `flow_field_action` + `jobspec_field_action` (issue #22): the full inbound lifecycle — Accept, Reject, Request Info, Add Note — driven entirely by native Zendesk controls. An agent (or a macro) sets the **TSANet Action** dropdown; a ZIS flow executes the action against the TSANet API and clears the field. No private app needed.

**This feature is off unless you turn it on**, and it is off on a first install. The
app decides from its own settings: leave **TSANet Action** and **TSANet Action Text**
blank and the deploy screen omits `flow_field_action`, `jobspec_field_action` and the
seven actions only that flow uses, then deploys everything else normally. Fill both in
and deploy again to add it. Nothing about the sidebar app changes either way — it never
reads these two fields, and performs Accept / Reject / Request Info / Add Note by
calling the TSANet API directly.

### Additional setup

1. Create two more custom ticket fields:
   - **TSANet Action** — dropdown with options `Accept` (tag `tsanet_action_accept`), `Reject` (`tsanet_action_reject`), `Request Info` (`tsanet_action_request_info`), `Add Note` (`tsanet_action_add_note`)
   - **TSANet Action Text** — text; holds the reject reason / info question / note body
2. Enter both Field IDs in the app's settings, plus **`tsanet_engineer_email`** — TSANet's Accept endpoint requires an `engineerEmail` from your registered domain, and agent emails fail validation. All three are required together: the two field IDs are a pair, and the deploy screen rejects one without the other rather than guessing.
3. **Deploy again** from the app. The bundle now carries the field-driven flow, and every job spec — including `jobspec_field_action` — is reinstalled, which is necessary because uploading a bundle orphans the specs from the previous one.
4. Optional but recommended: four macros ("TSANet: Accept", ...) that set the Action (and prompt for Action Text where relevant) for one-click agent UX.

> **Turning it back off.** Clearing the field IDs and redeploying omits the flow but does **not** uninstall `jobspec_field_action`, which would leave it registered and still intercepting `ticket.CustomFieldChanged` with no flow behind it. Pre-flight warns when it spots this. Uninstall it with `DELETE /api/services/zis/registry/job_specs/install?job_spec_name=zis:tsanet_connect:job_spec:jobspec_field_action`.

### Behavior

- **Success:** internal comment + TSANet Status updated + Action field cleared. **Add Note** writes an internal receipt comment carrying a `tsanet-note-id:<id>` marker instead of updating Status (see *Partner-only notes* below).
- **Failure** (wrong case state, missing text, no token): internal comment explaining, Action cleared, Status untouched. Details land in the Integration Log.
- **Guards:** the flow no-ops unless the changed field is TSANet Action with a non-empty action value — so the flow's own clears, status syncs, and any ZAF field writes never re-trigger it. Safe to run alongside the ZAF app (the two action paths are independent; see issue #22 for the coexistence analysis).

### Partner-only notes (native path, issue #69)

**Partner-only** means a note that reaches your TSANet partner but stays hidden from the end customer. On the native path (no ZAF app) it is driven by the **TSANet Action** field.

#### How a support agent sends a partner-only note

1. Open the TSANet ticket.
2. Type the note in the **TSANet Action Text** field.
3. Set **TSANet Action** to **Add Note** — or, in one click, apply the **`TSANet: Send partner-only note`** macro (it sets the dropdown for you; see admin setup below).
4. Submit the ticket.

The note is delivered to the partner **only** — the end customer never sees it. An **internal** receipt comment is added to the ticket (prefixed `[TSANet note sent to partner (partner-only)]`) so you have a record of exactly what was sent.

> **Important — partner-only is NOT in Zendesk's native reply menu.** Zendesk's built-in composer toggle offers only **Public reply** and **Internal note**, and that control is owned by Zendesk: an app or admin **cannot** add a third option to it. Agents will not find a "partner only" choice there, and a normal **public reply** reaches the end customer too. Partner-only is reached **only** through the **TSANet Action = Add Note** flow above (or the **`TSANet: Send partner-only note`** macro), or — if the ZAF app is installed — the app's own Add Note dialog (`tsanetgit/Zendesk_App#56`). Train agents to use the TSANet Action field / macro, not the native composer.

#### Under the hood

`flow_field_action` posts the Action Text to the partner (`POST /notes`) **without** writing a public Zendesk comment, then `FinishNote` records the **internal** receipt comment with a `tsanet-note-id:<id>` marker (the id comes from the `POST /notes` response, `$.ts.id`). The marker is what the ZAF note-mirror dedups on, so when the ZAF app is also installed the mirrored copy of the same note is suppressed — exactly one internal record either way, ZAF or no-ZAF.

#### Admin setup — the `TSANet: Send partner-only note` macro (optional)

Zendesk macros are per-instance Support config and **cannot ship in the ZIS bundle**, so each instance creates this macro once. Partner-only still works without it (set the **TSANet Action** dropdown to **Add Note** by hand); the macro is purely a one-click convenience.

Easiest route is the UI: **Admin Center → Workspaces → Macros → Add macro**, with one action setting **TSANet Action** to **Add Note**.

Via the API, substitute your **TSANet Action** field id for `FIELD_ID`. Unlike bundle
upload, this is a plain Support API endpoint and accepts OAuth, so use the
`$SETUP_TOKEN` bearer from Step 1 rather than an API token:

```bash
curl -X POST "https://YOURSUBDOMAIN.zendesk.com/api/v2/macros.json" \
  -H "Authorization: Bearer $SETUP_TOKEN" -H "Content-Type: application/json" \
  -d '{"macro":{"title":"TSANet: Send partner-only note","actions":[{"field":"custom_fields_FIELD_ID","value":"tsanet_action_add_note"}]}}'
```

The agent still types the note body into **TSANet Action Text** first (a macro can set the dropdown but cannot capture free-form text), then applies the macro and submits.

## Inbound comment forwarding — public reply → partner note (issue #34)

When an agent posts a **public reply** on a TSANet ticket, it is forwarded to the partner as a TSANet note, so the partner sees agent replies without anyone re-typing them. **Internal** comments are never forwarded — only public content reaches the partner.

```
Agent posts a PUBLIC reply on a TSANet ticket (inbound or outbound)
  → Zendesk trigger  (comment is public  AND  tag tsanet_inbound OR tsanet_outbound)
  → Zendesk webhook  (Basic auth)
  → ZIS inbound webhook   (source_system "zendesk", event_type "public_comment")
  → jobspec_forward_comment → flow_forward_comment
        GuardToken → GuardComment → GuardAuthor (agent/admin only)
        ForwardNote → action_ts_note → POST /notes   (connection tsanet_oauth)
```

The flow **reuses `action_ts_note`** (no new action). Loop-safe: the note mirror writes *internal* comments, which never re-fire this *public*-comment trigger.

- **Fail-closed author guard.** `flow_forward_comment` only forwards when `author_role` is `Agent`/`Admin` (the trigger sends `{{current_user.role}}`). An **End-user** public reply never forwards. **Gotcha:** `{{current_user.role}}` renders the literal **`Admin`** (not `Administrator`) — the guard matches `Agent`/`Admin` plus lowercase variants. ZIS `Choice` states only support `StringEquals` (not `StringMatches`), so each accepted value is listed explicitly.
- **Single-path rule (issue #38).** The ZAF app's public **Add Note** posts only the public comment and lets this trigger deliver it. It must **not** also `POST /notes` itself, or the partner gets the note twice.
- **Trigger scope.** It fires on `tsanet_inbound` **or** `tsanet_outbound` so public replies forward on both inbound and outbound cases.

### Setup (in addition to the inbound `collaboration_event` webhook in Deploy above)

```bash
# 1. Create the comment-forwarding inbound webhook (returns its own ingest path +
#    Basic creds + uuid — keep all three; same rotation/no-list-API rule as the main webhook)
curl -X POST "https://YOURSUBDOMAIN.zendesk.com/api/services/zis/inbound_webhooks/generic/tsanet_connect" \
  -H "Authorization: Bearer ZIS_OAUTH_TOKEN" -H "Content-Type: application/json" \
  -d '{"source_system":"zendesk","event_type":"public_comment"}'

# 2. Install its job spec (reinstall after EVERY bundle upload, like the others)
curl -X POST "https://YOURSUBDOMAIN.zendesk.com/api/services/zis/registry/job_specs/install?job_spec_name=zis:tsanet_connect:job_spec:jobspec_forward_comment" \
  -H "Authorization: Bearer ZIS_OAUTH_TOKEN"
```

Then, in **Zendesk Admin** (or via `/api/v2/webhooks` + `/api/v2/triggers`):
- A **webhook** with **Basic auth** = the step-1 ingest credentials, endpoint = the step-1 ingest URL, JSON.
- A **trigger** — conditions: *comment is public* AND (current tags include `tsanet_inbound` **or** `tsanet_outbound`); action: notify that webhook with body `{"token":"{{ticket.ticket_field_<TOKEN_FIELD_ID>}}","comment":"{{ticket.latest_public_comment}}","ticket_id":"{{ticket.id}}","author_role":"{{current_user.role}}"}`.

> Updating an existing trigger via the API **replaces** it — include the existing `actions` in the `PUT`, or you get `422 "Trigger must contain at least one action"`.

## Gotchas (each cost real debugging time — full record in issue #18)

- **Reinstall job specs after every bundle upload.** Uploads orphan existing installs; the flow silently stops firing.
- **Stale installed job specs keep running.** Job specs from older bundle generations stay installed even when no longer defined in the bundle, and will intercept events. List with `GET /api/services/zis/registry/tsanet_connect/job_specs`; uninstall with `DELETE .../job_specs/install?job_spec_name=...`.
- **Zendesk-side actions need the `zendesk` connection.** Path-only actions return `401 Couldn't authenticate you`. Use the auto-renewing **OAuth connection** (Prerequisites 2a–2c) — a static `bearer_token` connection goes stale (Zendesk OAuth tokens under new clients expire within 30 minutes), and `basic_auth` + API token dies with Zendesk's API-token retirement (2027-04-30).
- **Connection names are unique across types**, and `GET /api/services/zis/connections/{integration}?name=` only returns OAuth connections. If a create returns 409 but the typed GET 404s, check the other legacy types (`bearer_token`, `basic_auth`).
- **Request bodies are mustache-templated** (`{{$.x}}`). JSONPath-style keys (`"value.$": "$.x"`) inside `requestBody` fail with `Error Resolving JSON Params`.
- **Zendesk date fields reject ISO datetimes** (422 `InvalidValue`). The flow's Jq transform truncates `respondBy` to `YYYY-MM-DD`; keep that state if you modify the flow.
- **The Integration Log is the only debugging surface** (Admin Center → Apps and integrations → Integrations → Integration logs; there is no API). Each entry's `execution_states` + `details` pinpoints the failing state.
- **Zendesk search is eventually consistent.** `SearchTicket` can miss a ticket created seconds earlier. `GuardCreate` therefore allows only the creation event (`collaboration-request.created`) to create a ticket; any other event type that finds no ticket no-ops instead of creating a duplicate (issue #42, tightened for the Connect API's concurrent webhook delivery workers — deliveries are near-instant, unordered, and at-least-once, so a `note.created` racing ahead of the creation event is normal, not exceptional). The residual duplicate window is a *redelivered* creation event landing inside the search-consistency lag; TSANet redelivers only on failed deliveries, so this stays a theoretical test-only case.

## Reference (generated)

<!-- BEGIN GENERATED: bundle reference (do not edit by hand; run zis/gen_readme_reference.py) -->
> Generated from `tsanet_connect_bundle.json` by `zis/gen_readme_reference.py`.
> Do not edit between the markers; run the script to refresh.

Bundle `tsanet_connect` · template `2019-10-14` · 12 actions, 3 flows, 3 job specs.

### Job specs (event → flow)

| Job spec | event_source | event_type | Flow |
|---|---|---|---|
| `jobspec_field_action` | `support` | `ticket.CustomFieldChanged` | `flow_field_action` |
| `jobspec_forward_comment` | `zendesk` | `public_comment` | `flow_forward_comment` |
| `jobspec_handle_ping` | `tsanet` | `collaboration_event` | `flow_handle_ping` |

### Actions

| Action | Connection | Method | Endpoint |
|---|---|---|---|
| `action_create_ticket` | `zendesk` | POST | `/api/v2/tickets.json` |
| `action_get_collaboration` | `tsanet_oauth` | GET | `https://connect2.tsanet.org/v1/collaboration-requests/{requestToken}` |
| `action_search_ticket` | `zendesk` | GET | `/api/v2/tickets.json?external_id={requestToken}` |
| `action_ts_accept` | `tsanet_oauth` | POST | `https://connect2.tsanet.org/v1/collaboration-requests/{token}/approval` |
| `action_ts_info` | `tsanet_oauth` | POST | `https://connect2.tsanet.org/v1/collaboration-requests/{token}/information-request` |
| `action_ts_note` | `tsanet_oauth` | POST | `https://connect2.tsanet.org/v1/collaboration-requests/{token}/notes` |
| `action_ts_reject` | `tsanet_oauth` | POST | `https://connect2.tsanet.org/v1/collaboration-requests/{token}/rejection` |
| `action_update_ticket` | `zendesk` | PUT | `/api/v2/tickets/{ticket_id}.json` |
| `action_zd_finish_fail` | `zendesk` | PUT | `/api/v2/tickets/{ticket_id}.json` |
| `action_zd_finish_note_receipt` | `zendesk` | PUT | `/api/v2/tickets/{ticket_id}.json` |
| `action_zd_finish_status` | `zendesk` | PUT | `/api/v2/tickets/{ticket_id}.json` |
| `action_zd_get_ticket` | `zendesk` | GET | `/api/v2/tickets/{ticket_id}.json` |

### Flows (states)

- **`flow_field_action`** — StartAt `GuardField`
  - `AcceptCase` (Action) → `action_ts_accept`
  - `BuildNoteReceipt` (Action) → `Jq`
  - `CheckToken` (Choice)
  - `Dispatch` (Choice)
  - `Extract` (Action) → `Jq`
  - `FailComment` (Action) → `action_zd_finish_fail`
  - `FinishAccept` (Action) → `action_zd_finish_status`
  - `FinishInfo` (Action) → `action_zd_finish_status`
  - `FinishNote` (Action) → `action_zd_finish_note_receipt`
  - `FinishReject` (Action) → `action_zd_finish_status`
  - `GetTicket` (Action) → `action_zd_get_ticket`
  - `GuardField` (Choice)
  - `GuardValue` (Choice)
  - `InfoCase` (Action) → `action_ts_info`
  - `NoOp` (Succeed)
  - `NoteCase` (Action) → `action_ts_note`
  - `RejectCase` (Action) → `action_ts_reject`
- **`flow_forward_comment`** — StartAt `GuardToken`
  - `ForwardNote` (Action) → `action_ts_note`
  - `GuardAuthor` (Choice)
  - `GuardComment` (Choice)
  - `GuardToken` (Choice)
  - `NoOp` (Succeed)
- **`flow_handle_ping`** — StartAt `GetCollaboration`
  - `BuildSubmitter` (Action) → `Jq`
  - `CheckTicketExists` (Choice)
  - `CreateTicket` (Action) → `action_create_ticket`
  - `GetCollaboration` (Action) → `action_get_collaboration`
  - `GuardCreate` (Choice)
  - `NoOp` (Succeed)
  - `SearchTicket` (Action) → `action_search_ticket`
  - `TransformForUpdate` (Action) → `Jq`
  - `UpdateTicket` (Action) → `action_update_ticket`
<!-- END GENERATED: bundle reference -->
