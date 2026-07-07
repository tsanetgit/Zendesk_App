# TSANet Connect — ZIS Quick Start Guide

**Last updated:** June 2026  
**Time to complete:** ~30 minutes

This guide covers **connecting ZIS to the TSANet API and deploying the flow bundle** (Steps 1–5) — so ZIS flows can call TSANet without handling auth themselves. The method is **OAuth client credentials (Microsoft Entra)**: ZIS stores a long-lived client credential issued by TSANet and mints/renews short-lived tokens itself. Nothing scheduled, no server, no token-refresh automation ([issue #1](https://github.com/tsanetgit/Zendesk_App/issues/1)).

The integration is complete at the end of Step 5.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Zendesk Admin access | All setup calls authenticate with a short-lived OAuth token from the integration's OAuth client (Step 1). One exception: the bundle upload in Step 5 |
| TSANet-issued Entra client | Client ID + secret from TSANet, plus service principal onboarding (contact TSANet with your SP object ID) |
| ZIS OAuth client | Created in Zendesk Admin Center (see Step 2) |

---

## Architecture Overview

```
ZIS ↔ TSANet connection
│
└── OAuth client credentials (Entra)
      ZIS holds client_id + client_secret
      ZIS mints/renews short-lived tokens itself
      → nothing scheduled, no refresh automation
```

---

## Step 1 — Create the Integration's OAuth Client + Setup Token

All admin-level setup calls in this guide authenticate with a short-lived OAuth
token minted from a **confidential OAuth client**. This same client later backs the
`zendesk` connection (README Prerequisites), so you create it exactly once. Zendesk
API tokens are **not** used anywhere in this integration — Zendesk is retiring them
(no new tokens for new accounts after July 28, 2026; all tokens dead April 30, 2027;
background: [Zendesk_API_Credential_Decision.md](Zendesk_API_Credential_Decision.md)).

**1a. Create the client** (sign in as the dedicated service user the integration
should act as — tokens from this client act as that user):

1. Go to **Admin Center → Apps and integrations → APIs → OAuth clients**
2. Click **Add OAuth client** and fill in:
   - **Client name:** `TSANet Connect Integration`
   - **Identifier:** `tsanet_zendesk`
   - **Client kind:** **Confidential** ← required; the client_credentials grant rejects public clients
   - **Redirect URLs:** `https://yoursubdomain.zendesk.com` (placeholder — not used)
3. Click **Save**, then copy the **Secret** field that appears — the full secret is
   shown **only once** (afterwards only the first characters are visible). If you
   lose it, regenerating shows it truncated — delete and recreate the client instead.

**1b. Mint a setup token** (~30-minute lifetime; re-run this if setup takes longer):

```bash
SETUP_TOKEN=$(curl -s -X POST "https://YOURSUBDOMAIN.zendesk.com/oauth/tokens" \
  -H "Content-Type: application/json" \
  -d '{"grant_type":"client_credentials","client_id":"tsanet_zendesk","client_secret":"YOUR_CLIENT_SECRET","scope":"read write"}' \
  | jq -r '.access_token')
```

---

## Step 2 — Create a ZIS OAuth Client

ZIS needs an OAuth client to issue short-lived tokens for managing connections.

1. Go to **Admin Center → Apps and integrations → APIs → OAuth clients**
2. Click **Add OAuth client**
3. Fill in:
   - **Client name:** `tsanet_zis_client`
   - **Description:** TSANet ZIS integration OAuth client
   - **Company:** TSANet
   - **Redirect URLs:** `https://yoursubdomain.zendesk.com` (placeholder — not used)
4. Click **Save** and copy the **Client ID** shown

---

## Step 3 — Create the ZIS Integration

The ZIS integration is a named container for TSANet's connections and flows within Zendesk.

Run this once from your terminal (replace values):

```bash
curl -s -X POST \
  "https://YOURSUBDOMAIN.zendesk.com/api/services/zis/registry/tsanet_connect" \
  -H "Authorization: Bearer $SETUP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"description": "TSANet Connect integration"}'
```

The integration name (`tsanet_connect`) goes in the **URL path** under `/registry/`; the body carries only the description. A `200 OK` confirms the integration was created.

> If you get a 409 Conflict, the integration already exists — that's fine, proceed to Step 4.

---

## Step 4 — Connect ZIS to TSANet

This is the step the rest of the guide exists to support: registering a connection that lets ZIS flows call the TSANet API. All commands below authenticate with a **ZIS OAuth token** — get one first (uses the Client ID from Step 2):

```bash
ZIS_TOKEN=$(curl -s -X POST \
  "https://YOURSUBDOMAIN.zendesk.com/api/v2/oauth/tokens" \
  -H "Authorization: Bearer $SETUP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"token":{"client_id":"YOUR_ZIS_CLIENT_ID","scopes":["read","write"]}}' | jq -r '.token.full_token')
```

Requires the TSANet-issued Entra client (see Prerequisites). ZIS stores the credential and handles all token minting and renewal itself.

**4a. Register the OAuth client** (the API scope goes in `default_scopes`):
```bash
curl -s -X POST \
  "https://YOURSUBDOMAIN.zendesk.com/api/services/zis/connections/oauth/clients/tsanet_connect" \
  -H "Authorization: Bearer $ZIS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "tsanet_entra",
    "grant_type": "client_credentials",
    "client_id": "YOUR_ENTRA_CLIENT_ID",
    "client_secret": "YOUR_ENTRA_CLIENT_SECRET",
    "token_url": "https://login.microsoftonline.com/TENANT_ID/oauth2/v2.0/token",
    "default_scopes": "api://AUDIENCE/.default"
  }'
```
TSANet provides the `TENANT_ID`, `AUDIENCE`, and your client credentials. Paste the secret **verbatim** — Entra secrets can begin with punctuation, and trimming it breaks auth with `AADSTS7000215`.

**4b. Create the connection** (no browser or admin-consent step):
```bash
curl -s -X POST \
  "https://YOURSUBDOMAIN.zendesk.com/api/services/zis/connections/oauth/start/tsanet_connect" \
  -H "Authorization: Bearer $ZIS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"oauth_client_name": "tsanet_entra", "name": "tsanet_oauth"}'
```
The response contains a `redirect_url` with a `verification_code`. **GET that URL (with the same `$ZIS_TOKEN` bearer) to complete creation** — required even for client credentials.

**4c. Verify** — the connection should hold a live `access_token` and a `token_expiry` about an hour out:
```bash
curl -s -H "Authorization: Bearer $ZIS_TOKEN" \
  "https://YOURSUBDOMAIN.zendesk.com/api/services/zis/connections/tsanet_connect?name=tsanet_oauth"
```
ZIS renews the token automatically when it expires. Continue to Step 5 to deploy the flow bundle that puts this connection to work.

> Gotcha: to change the stored credential later, the endpoint is **`PATCH`** `/api/services/zis/connections/oauth/clients/tsanet_connect/{uuid}` — `PUT` returns 405.

> An earlier revision of this guide also documented a legacy bearer-token method (a static TSANet JWT kept alive by a scheduled GitHub Actions refresh job). It has been removed to avoid confusion; if you ever need it, it lives in this file's git history.

---

## Step 5 — Deploy the Flow Bundle

The connection from Step 4 does nothing by itself — the flows that create and update Zendesk tickets live in the **flow bundle** under [`zis/`](zis/). Three calls deploy it (full detail, per-instance substitutions, and gotchas in [`zis/README.md`](zis/README.md)):

1. **Upload** [`zis/tsanet_connect_bundle.json`](zis/tsanet_connect_bundle.json) (after substituting your custom field IDs) to `POST /api/services/zis/registry/tsanet_connect/bundles`
2. **Create the inbound webhook** (`source_system: tsanet`, `event_type: collaboration_event`) and keep the returned ingest URL + Basic credentials
3. **Install the job spec** — and re-run this after *every* bundle upload, uploads orphan installs

> **Bundle upload is the one basic-auth exception.** The bundles endpoint currently
> rejects OAuth outright (401 "Authorization failed due to OAuth being disabled for
> this API request", verified 2026-07-07), so this single call cannot use
> `$SETUP_TOKEN`. Temporarily enable password access (**Admin Center → Apps and
> integrations → APIs → Zendesk API → Settings → Password access**), upload with
> `-u "YOUR_EMAIL:YOUR_PASSWORD"`, then disable password access again. Re-test the
> endpoint with OAuth periodically — Zendesk is expected to enable it before API
> tokens are fully retired.

Also create the **OAuth `zendesk` connection** the bundle's Zendesk-side actions require (see the README's prerequisites, steps 2b–2c — it reuses the Step 1 client, registering a ZIS client_credentials connection against your own instance's `/oauth/tokens`).

> **Inbound push is live.** TSANet → ZIS webhook delivery uses the `callbackAuth` capability ([issue #2](https://github.com/tsanetgit/Zendesk_App/issues/2)), delivered in API **v3.1.0** and validated on Beta (authenticated deliveries return 200 and create tickets). Register the member's webhook subscription with `callbackUrl` = the ingest URL and a `callbackAuth` of type `BASIC` carrying the ingest credentials. You can still exercise the pipeline manually by POSTing a `WebhookPayload`-shaped body to the ingest URL with its Basic credentials.

### Inbound comment forwarding (optional, recommended)

The bundle can also forward an agent's **public reply** to the partner as a TSANet note (issue #34) — so the partner sees agent replies automatically. **Internal** comments are never forwarded; only public replies reach the partner. It needs a second inbound webhook (`source_system: zendesk`, `event_type: public_comment`) plus a Zendesk webhook + trigger. Full setup is in [`zis/README.md` → *Inbound comment forwarding*](zis/README.md).

**You are finished here.** The core integration (ZIS connection + flow bundle, Steps 1–5) is complete.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| ZIS reports `AADSTS7000215` (invalid client secret) but the same secret works elsewhere | The stored value is corrupted (paste artifact, trimmed leading punctuation) — re-send it verbatim via `PATCH /api/services/zis/connections/oauth/clients/tsanet_connect/{uuid}` |
| Updating the OAuth client returns 405 | Use `PATCH`, not `PUT`, on the client endpoint |
| Connection created but no `access_token` | The `verification_code` exchange step was skipped — GET the `redirect_url` from the start response with the ZIS bearer |

---

## Key Concepts

**How does ZIS authenticate to TSANet?**  
The ZIS OAuth connection stores your TSANet-issued Entra client credential and exchanges it for short-lived access tokens via the client credentials grant. ZIS renews expired tokens automatically, so every ZIS flow that calls the TSANet API gets a valid token without handling any authentication logic itself.

**How does a support agent send a partner-only note (no ZAF app)?**  
A partner-only note reaches the TSANet partner but stays hidden from the end customer. The agent:

1. Types the note in the **TSANet Action Text** field.
2. Sets **TSANet Action** to **Add Note** — or applies the one-click **`TSANet: Send partner-only note`** macro (admins create this macro once; it sets the dropdown for the agent).
3. Submits the ticket.

The flow posts the note to the partner (`POST /notes`) with **no** public comment, and adds an **internal** receipt comment (prefixed `[TSANet note sent to partner (partner-only)]`, carrying a `tsanet-note-id` marker) so there is a Zendesk record whether or not the ZAF app is installed. This is the native equivalent of the ZAF app's "Partner only" tier ([#56](https://github.com/tsanetgit/Zendesk_App/issues/56)); full detail and the macro-creation command are in [`zis/README.md`](zis/README.md). **Important:** Zendesk's native composer toggle is only *Public reply* / *Internal note* and **cannot be extended**, so partner-only must come from the TSANet Action field or the **`TSANet: Send partner-only note`** macro, never the native reply menu.

**What lands on an inbound ticket?**  
The created ticket carries the TSANet token, status, and partner company, plus — when the collaboration includes submitter contact — a `Submitter: Name <email>` line in the description and opening comment ([#57](https://github.com/tsanetgit/Zendesk_App/issues/57)), so the partner engineer who opened the case is visible without opening the sidebar.

---

## Important Limitations

**ZIS scheduled polling is retired**  
An earlier design included a ZIS flow (`flow_poll_tsanet`) intended to poll TSANet for inbound cases on a schedule. This never functioned due to three layered failures (clock ticket required `new` status, no `requestToken` in automation payload, JWT expiry during execution). The flow remains installed but is permanently dormant — both Zendesk automations that triggered it have been disabled. Inbound case sync is now handled by:
- **ZIS push delivery (primary)** — TSANet POSTs each event to the ZIS ingest webhook, secured by `callbackAuth` (see below)
- **ZAF background poller (fallback)** — runs every 1 minute while any agent has Zendesk open; defers to push and only backfills a ticket push didn't already create

**ZIS inbound webhook — resolved (push is live)**  
This was previously blocked: ZIS inbound webhook flows require an `Authorization` header on every POST, but TSANet's webhook system sent only an HMAC-SHA256 signature, so direct delivery returned 401. TSANet added the `callbackAuth` capability to its webhook registration API (delivered in Connect API **v3.1.0**), which closes the gap — [issue #2](https://github.com/tsanetgit/Zendesk_App/issues/2) is closed. Register the member's webhook subscription with a `callbackAuth` of type `BASIC` carrying the ingest credentials; TSANet then attaches Basic Auth on every delivery alongside the HMAC signature, and ZIS accepts the authenticated request. Validated end to end on Beta (deliveries return 200 and create exactly one ticket per case).

**Zendesk Views API cannot set custom field columns**  
If you create or modify a Zendesk view via the API and include `custom_field_XXXXXXX` column IDs in `execution.columns`, the API accepts the request without error but silently reverts to the original columns. Custom field columns on views must be configured manually in **Admin Center → Workspaces → Views**.
