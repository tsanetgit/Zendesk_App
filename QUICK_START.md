# TSANet Connect for Zendesk: Quick Start

**App version:** see the [latest release](https://github.com/tsanetgit/Zendesk_App/releases/latest). Always install the newest ZIP; this guide is not pinned to a version.
**Last updated:** July 2026
**Time to complete:** about half a day of focused work

This is the single install guide for the TSANet Connect integration. It covers both halves:

- **ZIS** (Zendesk Integration Services), the server-side plumbing that creates and updates tickets when TSANet sends you an event.
- **ZAF** (the sidebar app), the panel your agents work in on every ticket.

There is **one download**. The ZIS flow bundle ships inside the app ZIP, and the app is what deploys it, so you install the app before you deploy the bundle. There is no separate ZIS package to fetch and no API token to create.

> **This guide replaces `ZAF_Quick_Start.md` and `ZIS_Quick_Start.md`.** Those two described the same install from two ends and could not be followed independently once the bundle deploy moved into the app.

---

## Before You Start

### From TSANet

Two credentials that do two different jobs. You need both, and they are not interchangeable.

| Credential | Used by | What it is |
|---|---|---|
| **API user** (username + password) | The **ZAF sidebar app**, from the agent's browser | A dedicated account belonging to the integration, not to a person. Email membership@tsanet.org with the subject "API Credentials Request: Zendesk Integration" |
| **Microsoft Entra client** (client ID + secret) | **ZIS**, server-side | A member-specific app registration in TSANet's Entra tenant, created by TSANet. Also requires service principal onboarding: send TSANet your service principal's object ID |

TSANet also sends you `TENANT_ID` and `AUDIENCE` (both non-secret GUIDs, used in Step 2). These differ between BETA and PRODUCTION, so do not reuse BETA values against PRODUCTION.

The Entra secret is the opaque **Value** (it looks like `xxx~xxxx...`), not a GUID. If you received a GUID, that is the Secret ID; ask TSANet for the Value.

**You do not need:** a TSANet admin account, or any Entra admin-consent or app-role grant on your side.

### From Zendesk

- **Administrator access.** Not just agent. Several steps are admin-only.
- **A paid plan, Suite Professional or higher.** ZIS is not available on lower tiers, and trial accounts cannot be used.
- **Four custom ticket fields** (a fifth is optional). Create them in **Admin Center > Objects and rules > Tickets > Fields**.

| Field name | Type | What it stores |
|---|---|---|
| TSANet Token | Text | The unique ID linking this ticket to a TSANet case |
| TSANet Status | Dropdown | Values: `tsanet_status_open`, `tsanet_status_accepted`, `tsanet_status_information`, `tsanet_status_rejected`, `tsanet_status_closed` |
| TSANet Partner | Text | The partner company you are collaborating with |
| TSANet Respond By | Date | The acknowledgment SLA deadline (auto-cleared on acknowledgment) |
| TSANet Tokens (Multi) | Text | *Optional.* A list of IDs for tickets carrying more than one case |

> **You do not need to write down the field IDs.** Create the fields and move on. In Step 3 the app reads them out of your instance and writes them into its own settings, and in Step 4 it substitutes them into the bundle for you. The ID settings are deliberately blank at install time.

### The five steps at a glance

1. **Register Zendesk with ZIS.** Create the OAuth client and the integration container that ZIS needs to manage itself.
2. **Connect ZIS to TSANet.** Register the Entra client-credentials connection so ZIS can call the TSANet API without handling authentication itself.
3. **Install the ZAF sidebar app.** The panel agents use, and the tool that deploys the bundle in Step 4.
4. **Deploy the ZIS flow bundle.** This is what actually creates and updates tickets. Steps 1 and 2 only build the plumbing it runs on.
5. **Test everything before going live.**

---

## Step 1: Register Zendesk with ZIS

Two pieces of one-time Zendesk-side setup. You create one OAuth client; ZIS creates the second one for you.

### 1a. Create the integration's OAuth client and a setup token

Every setup call in this guide authenticates with a short-lived **setup token** minted from a confidential OAuth client. This same client later backs the `zendesk` connection in Step 4a, so you create it exactly once.

No Zendesk API token is used anywhere in this integration. Zendesk is retiring them: none for accounts created on or after **2026-07-28**, none for any account after **2026-10-27**, and all existing tokens stop working on **2027-04-30**. Background: [Zendesk_API_Credential_Decision.md](Zendesk_API_Credential_Decision.md).

Sign in to Admin Center **as the dedicated service user the integration should act as** (tokens from this client act as that user), then go to **Apps and integrations > APIs > OAuth clients > Add OAuth client**:

- **Client name:** `TSANet Connect Integration`
- **Identifier:** `tsanet_zendesk`
- **Client kind:** **Confidential.** Required. The `client_credentials` grant rejects public clients with `unauthorized_client`.
- **Redirect URLs:** `https://YOURSUBDOMAIN.zendesk.com` (placeholder, not used)

Click **Save**, then copy the **Secret**. The full secret is shown **only once**. Regenerating later displays it truncated, so if you lose it, delete and recreate the client. The client must also be confidential **at creation**: changing `kind` afterwards regenerates the secret and only ever shows it truncated.

Mint a setup token (roughly a 30-minute lifetime, so re-run this if setup takes longer):

```bash
SETUP_TOKEN=$(curl -s -X POST "https://YOURSUBDOMAIN.zendesk.com/oauth/tokens" \
  -H "Content-Type: application/json" \
  -d '{"grant_type":"client_credentials","client_id":"tsanet_zendesk","client_secret":"YOUR_CLIENT_SECRET","scope":"read write"}' \
  | jq -r '.access_token')
```

### 1b. Create the ZIS integration container

A named bucket inside Zendesk's ZIS platform that holds all TSANet resources: connections, the flow bundle, and webhooks.

```bash
curl -s -w "\nHTTP %{http_code}\n" -X POST \
  "https://YOURSUBDOMAIN.zendesk.com/api/services/zis/registry/tsanet_connect" \
  -H "Authorization: Bearer $SETUP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"description": "TSANet Connect integration"}'
```

The integration name (`tsanet_connect`) goes in the **URL path**; the body carries only the description. It is case-sensitive. `HTTP 200` confirms it was created, and `HTTP 409` means it already exists, which is fine. (The app also ensures this container exists when you deploy in Step 4, but Step 2 needs it to be there first.)

> **If you get `400 the integration: tsanet_connect is not available for upsert by this account`,** that is expected, not a fault on your side. Zendesk requires ZIS integration names to be **globally unique across all Zendesk accounts**, and `tsanet_connect` is already registered elsewhere ([Zendesk: building your first ZIS integration](https://developer.zendesk.com/documentation/integration-services/zis-tutorials/getting-started/building-your-first-zis-integration/)). Pick your own, for example `yourcompany_tsanet_connect`, and register that instead. Lowercase letters, digits, underscore and hyphen, 1-64 characters. Then **use your name everywhere `tsanet_connect` appears in this guide**, including the connection calls in Steps 2 and 4a and the webhook call in Step 4d, and enter it in the app's `tsanet_integration_name` setting in Step 3c so the bundle is built for your container. Requires app **v1.0.61 or later** (`tsanetgit/Zendesk_App#174`).

> **Do not continue until you see 200 or 409.** This is the one step whose failure surfaces later and in a form that points somewhere else: with no container, Step 2b returns `401 Authorization failed due to integration mismatch`, which reads like a bad Entra credential rather than a missing container. The usual cause is permissions. ZIS registry endpoints are admin-only, and a `client_credentials` token acts as the user its OAuth client was created under, so a non-admin mints tokens successfully in Step 1a and is refused only here. If that is what happened, recreate the Step 1a client while signed in as an administrator and mint the setup token again.

Creating the container also creates an OAuth client for it, named `zis_tsanet_connect`. The 200 response carries it as `zendesk_oauth_client`. **This is the client the ZIS token must come from**, and it is the only one that will work:

```json
"zendesk_oauth_client": { "id": 1234567890123, "identifier": "zis_tsanet_connect", "secret": "..." }
```

> **A client you create yourself cannot authenticate a ZIS call.** A ZIS token is bound to exactly one integration, and the binding comes from the client that minted it. A token from any other client, however much access that client has, is refused with `401 Authorization failed due to integration mismatch` on every ZIS management endpoint. Only `zis_<integration-name>` works.

Finally, mint the ZIS token used by every ZIS management call in Steps 2 and 4. Pass the numeric `id` from `zendesk_oauth_client` above, not the identifier string, and not the client from Step 1a:

```bash
ZIS_TOKEN=$(curl -s -X POST \
  "https://YOURSUBDOMAIN.zendesk.com/api/v2/oauth/tokens" \
  -H "Authorization: Bearer $SETUP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"token":{"client_id":ZIS_CLIENT_NUMERIC_ID,"scopes":["read","write"]}}' | jq -r '.token.full_token')
```

The mint needs only the numeric id, not the client's secret, so if you did not keep the create response you can look the id up at any time. Find the entry whose `identifier` is `zis_tsanet_connect`:

```bash
curl -s -H "Authorization: Bearer $SETUP_TOKEN" \
  "https://YOURSUBDOMAIN.zendesk.com/api/v2/oauth/clients.json" \
  | jq '.clients[] | {id, identifier}'
```

---

## Step 2: Connect ZIS to TSANet

This registers the connection that lets ZIS flows call the TSANet API on their own. The method is **OAuth client credentials (Microsoft Entra)**: ZIS stores the long-lived client credential TSANet issued you and mints and renews its own short-lived tokens. Nothing is scheduled and there is no refresh job to maintain (`tsanetgit/Zendesk_App#1`).

### 2a. Pre-flight the Entra values (optional, recommended)

Confirm the credential works before you store it anywhere:

```bash
curl -s -X POST "https://login.microsoftonline.com/TENANT_ID/oauth2/v2.0/token" \
  -d grant_type=client_credentials \
  -d client_id=YOUR_ENTRA_CLIENT_ID \
  --data-urlencode "client_secret=YOUR_ENTRA_CLIENT_SECRET" \
  -d scope=AUDIENCE/.default
```

A response containing `access_token` means the values are correct. The two failures you are likely to see:

- `AADSTS7000215` means a wrong secret. Check you have the **Value**, not the Secret ID, and that nothing was trimmed.
- `AADSTS500011` means a wrong scope format or a wrong `AUDIENCE`.

### 2b. Register the OAuth client

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
    "default_scopes": "AUDIENCE/.default"
  }'
```

> **Scope format.** Use the bare Connect-app client ID GUID in `default_scopes`, that is `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx/.default`, using the `AUDIENCE` value TSANet gives you. Do **not** prefix it with `api://`. That form fails with `AADSTS500011: resource principal not found`, because the Connect app's Application ID URI is not published. The same applies when testing the token request in Postman or curl.

> **Paste the secret verbatim.** Entra secrets can begin with punctuation, and trimming a leading character breaks auth with `AADSTS7000215`.

Where each value comes from:

| Value | What it is | Who provides it |
|---|---|---|
| `TENANT_ID` | TSANet's Entra tenant ID (a GUID), used in the token URL | TSANet |
| `AUDIENCE` | The client ID of the TSANet Connect app registration for your environment. The scope is `AUDIENCE/.default` | TSANet |
| `client_id` / `client_secret` | Your member-specific app registration in TSANet's Entra tenant | TSANet |
| `$ZIS_TOKEN` | A ZIS OAuth bearer token for **your own** Zendesk instance (Step 1b) | You |

Per-environment API hosts: BETA is `connect2.tsanet.net`, PRODUCTION is `connect2.tsanet.org`. `TENANT_ID` and `AUDIENCE` are provided by TSANet at onboarding and differ per environment.

### 2c. Create the connection

No browser or admin-consent step is needed for client credentials:

```bash
curl -s -X POST \
  "https://YOURSUBDOMAIN.zendesk.com/api/services/zis/connections/oauth/start/tsanet_connect" \
  -H "Authorization: Bearer $ZIS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"oauth_client_name": "tsanet_entra", "name": "tsanet_oauth"}'
```

The response contains a `redirect_url` carrying a `verification_code`. **GET that URL** with the same `$ZIS_TOKEN` bearer to complete creation. This step is required even for client credentials.

### 2d. Verify

The connection should hold a live `access_token` and a `token_expiry` about an hour out:

```bash
curl -s -H "Authorization: Bearer $ZIS_TOKEN" \
  "https://YOURSUBDOMAIN.zendesk.com/api/services/zis/connections/tsanet_connect?name=tsanet_oauth"
```

ZIS renews the token automatically when it expires.

> **Gotcha:** to change the stored credential later, the endpoint is **`PATCH`** `/api/services/zis/connections/oauth/clients/tsanet_connect/{uuid}`. `PUT` returns 405.

---

## Step 3: Install the ZAF Sidebar App

The app is the panel agents work in, and it is also how you deploy the ZIS bundle in Step 4, so it goes in first. It is distributed as a pre-built ZIP and installed privately. There is no Zendesk Marketplace listing.

**Requires v1.0.53 or later**, which is the release where the app reads your field IDs out of the instance instead of asking you to type them. Earlier releases either could not open the deploy screen at all (v1.0.49 and v1.0.50 declared the nav bar location without shipping its icon, `tsanetgit/Zendesk_App#131`) or blocked fresh installs (v1.0.51, `tsanetgit/Zendesk_App#134`).

### 3a. Download and verify the package

Get `tsanet-connect-v<version>.zip` from the [latest release](https://github.com/tsanetgit/Zendesk_App/releases/latest).

Every release is published with a build-provenance attestation and a SHA-256 checksum, both on the same release page, so you can confirm the ZIP is the one TSANet built rather than a file altered in transit or substituted elsewhere.

```bash
# Provenance (requires the GitHub CLI)
gh attestation verify tsanet-connect-v<version>.zip --repo tsanetgit/Zendesk_App

# Or checksum only, run in the folder holding the ZIP and checksums.txt
shasum -a 256 -c checksums.txt
```

Expect a line confirming the attestation was verified against `tsanetgit/Zendesk_App`, or `tsanet-connect-v<version>.zip: OK` from the checksum check.

> **If verification fails, do not install the package.** A failure means the file does not match what TSANet published. Re-download from the release page and try once more. If it still fails, contact membership@tsanet.org before proceeding. A mismatch is worth reporting even if the re-download then succeeds.

### 3b. Install it

1. Go to **Admin Center > Apps and integrations > Zendesk Support Apps**
2. Click **Upload private app**
3. Name it `TSANet Connect`
4. Upload the ZIP and click **Upload**

Zendesk validates the package and shows the installation settings screen.

### 3c. Configure the settings

| Setting | Value | Required |
|---|---|---|
| **TSANet API username** | Your TSANet API user email, for example `api@yourcompany.com` | Yes |
| **TSANet API password** | The password for that account | Yes |
| **TSANet environment** | `BETA` or `PRODUCTION` | Yes |
| **All field ID settings** | **Leave blank.** Step 3d fills them in | No |
| **Allowed action roles** | Comma-separated Zendesk role names permitted to invoke TSANet actions, for example `admin, Support Lead`. Empty means all agents | No |
| **TSANet integration name** | **Leave as `tsanet_connect`** unless Step 1b forced you onto a different name, in which case enter the exact name you registered | No |

`BETA` maps to `connect2.tsanet.net` and `PRODUCTION` to `connect2.tsanet.org`. Set it to match where your account is provisioned.

The **TSANet API password** is a Zendesk **secure setting**: it is stored encrypted, never reaches the front end, and requests using it are proxied server-side by Zendesk.

> **Allowed action roles is a UI gate, not a security boundary.** It controls which roles see and can click the TSANet action buttons. The TSANet Connect API and its credential remain the real authorization control. Role matching is case-insensitive against the agent's Zendesk role name.

Click **Install**.

### 3d. Detect the field IDs

Open **TSANet Connect** from the left nav bar in Zendesk Support and click **Detect field IDs**.

It reads this instance's ticket fields, matches them by name, and shows you exactly what it found (name, id, and type) before anything is saved. Click **Apply** to write them into the app's settings.

It refuses rather than guesses:

- Two fields sharing a name are reported as ambiguous, and it names both IDs.
- A name match with the wrong type is refused. **TSANet Status** must be a dropdown and **TSANet Respond By** must be a date.
- A missing required field is called out, with where to create it. A missing optional one is fine.

Until this is done the sidebar tells any agent who opens a ticket that TSANet Connect is not configured yet, rather than appearing to work and quietly doing nothing.

### 3e. Confirm the sidebar renders

Open a ticket with **no** TSANet collaboration on it. The **TSANet Connect** panel should appear collapsed to a slim bar (about 44px) reading **"No active TSANet cases"** with a **+ New** button. Click **+ New** and the panel should expand and open the New Collaboration search dialog.

If the sidebar shows an error instead of the compact bar, re-check the API credentials and that the environment setting matches where your account is provisioned.

---

## Step 4: Deploy the ZIS Flow Bundle

The connection from Step 2 does nothing by itself. The flows that create and update Zendesk tickets live in the **ZIS flow bundle**, a single JSON file that ships **inside the app ZIP you just installed**. You do not download or edit it separately.

Full technical reference, every flow, and every gotcha: [`zis/README.md`](zis/README.md).

### 4a. Create the `zendesk` connection

The bundle's Zendesk-side actions (creating, searching, and updating tickets) do not authenticate themselves. They need an **OAuth connection named `zendesk`**. It stores no long-lived secret: ZIS mints short-lived tokens from an OAuth client on your own instance and renews them itself.

The client is the one from **Step 1a** (`tsanet_zendesk`, confidential, owned by the dedicated service user), same identifier and same secret.

```bash
curl -s -X POST \
  "https://YOURSUBDOMAIN.zendesk.com/api/services/zis/connections/oauth/clients/tsanet_connect" \
  -H "Authorization: Bearer $ZIS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "zendesk_self",
    "grant_type": "client_credentials",
    "client_id": "tsanet_zendesk",
    "client_secret": "YOUR_CLIENT_SECRET",
    "token_url": "https://YOURSUBDOMAIN.zendesk.com/oauth/tokens",
    "default_scopes": "read tickets:write"
  }'

curl -s -X POST \
  "https://YOURSUBDOMAIN.zendesk.com/api/services/zis/connections/oauth/start/tsanet_connect" \
  -H "Authorization: Bearer $ZIS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"oauth_client_name": "zendesk_self", "name": "zendesk"}'
```

GET the returned `redirect_url` with the same `$ZIS_TOKEN` bearer to complete creation, the same verification step as Step 2c.

> `read tickets:write` is the **minimal** scope for the bundle's seven Zendesk-side calls. `tickets:read` alone is not enough: `/api/v2/search.json` returns 403 under it and `SearchTicket` breaks, and there is no `search:read` scope. Create and update need `tickets:write`.

> **Migrating an existing install?** Connection names are unique across types, so delete the old basic-auth `zendesk` connection first, then create this one under the same name. The bundle needs no changes.

### 4b. Check the values the app will substitute

You do not edit the bundle JSON. The app fills these in from its own settings at deploy time, and refuses to upload if any of them is still a placeholder. Use this table to confirm the settings are right before you deploy, and to know what to look at if a flow later misbehaves.

| What | Where it appears | Note |
|---|---|---|
| Custom field IDs | ticket create / search / update actions | Filled in by **Detect field IDs** in Step 3d |
| API host | **every** TSANet API action | Ships pointed at Production (`connect2.tsanet.org`); Beta is `connect2.tsanet.net`. It appears in all the TSANet API actions, not just the first, and they move together |
| `engineerEmail` | the Accept action | Your TSANet API user's email. It must be on your member-registered domain: TSANet's Accept endpoint rejects any other domain |
| OAuth connection name | every TSANet API action | Ships as `tsanet_oauth`. This only differs if you named the Step 2c connection something else |

### 4c. Deploy the bundle

1. In Zendesk Support, open **TSANet Connect** from the left nav bar.
2. Read the **Current state** card at the top of the screen.
3. Check the **Pre-flight** results. All three must pass before the button enables.
4. Click **Deploy bundle**.

The app substitutes your per-instance values, uploads the bundle, **installs every job spec in it**, then reads the registry back so you can see what is actually installed. Success is judged by that read-back, not by the upload response.

> **The Current state card tells you whether you need to deploy at all** (app **v1.0.63 or later**). It has three rows:
>
> | Row | What it tells you |
> |---|---|
> | Bundle | Whether the bundle registered on this instance matches what this app would deploy |
> | App version | Which version of the app is installed here |
> | Latest release | Whether a newer version has been published |
>
> Only the Bundle row bears on the decision, and it is driven by comparing the bundle's **actual contents**, never by comparing version numbers. That distinction is deliberate: between v1.0.54 and v1.0.60 there were six releases in which the ZIS bundle did not change once, so a version comparison would have asked every member to redeploy six times for no functional change. The two version rows are reference only and can never disable the Deploy button.
>
> The Bundle row distinguishes three answers, because the remedy differs for each. *Older than the one this app ships* means a genuinely different bundle generation, so deploy. *App settings changed since it was deployed* means the bundle is the right generation but was built with different values, so deploy to pick them up. *Not deployed yet* means the registry has nothing to compare.
>
> **"Matches what this app would deploy" is not the same as "no deploy needed."** It establishes what is registered and nothing about whether the job specs are installed. A deploy interrupted between the upload and the installs leaves a matching registry on an integration that processes no events. Installed state is what Pre-flight and the post-deploy read-back are for.

> **You must be a Zendesk administrator.** The nav bar screen is visible to any agent, so the app's own role check is a convenience gate rather than a security boundary. The boundary is server-side: Zendesk documents the ZIS registry endpoints as [Allowed for: Admins](https://developer.zendesk.com/api-reference/integration-services/registry/bundles/), and TSANet has now tested that claim. On 2026-07-28 a session holding the `Staff` custom role, the most privileged non-admin role on the test instance, was refused with `403 Only admin user is allowed` on the bundles endpoint and `403 Forbidden` on `PUT /api/v2/apps/installations/{id}`, which is what **Apply** on the same screen writes to. That is one role, on one instance, on one day (`tsanetgit/Zendesk_App#125`): evidence that the vendor documentation is accurate, not an exhaustive proof.

> **Why the app and not a curl command.** The bundles endpoint rejects OAuth outright (401 "Authorization failed due to OAuth being disabled for this API request", re-verified 2026-07-27), so neither `$SETUP_TOKEN` nor `$ZIS_TOKEN` works on it. The only credentials it accepts are a Zendesk API token and your own signed-in admin session, and Zendesk is withdrawing API tokens on the timeline in Step 1a. The app runs on your admin session, so there is nothing to create, store, or rotate for this step.

> **Deploying replaces the installed bundle**, and an upload orphans the job specs installed from the previous one. The app reinstalls all of them immediately and verifies the result, but the integration is briefly inactive in between, so do not close the tab mid-run. If a step fails, the screen names it and offers **Retry all**. If the read-back reports job specs that are installed but not in the current bundle, those are stale orphans from an older generation and they still intercept events: uninstall them with `DELETE /api/services/zis/registry/job_specs/install?job_spec_name=zis:tsanet_connect:job_spec:<name>`.

> **Job specs no longer need a manual install.** Earlier revisions of this guide had you `POST .../job_specs/install` for `jobspec_handle_ping` by hand, and repeat it after every upload. The app now installs every job spec the bundle declares, on every deploy, and confirms it. Skip any older instructions that ask you to do it with curl.

### 4d. Create the inbound webhook, then subscribe TSANet to it

This is **two API calls, both made by you.** Nothing here is an email to TSANet, and nobody at TSANet does anything on your behalf.

1. **Zendesk** creates the address TSANet will post to, and hands you a credential guarding it.
2. **TSANet** is told to post to that address, using that credential.

The second call simply carries across the values the first one returned.

#### Call 1: create the ingest URL (to Zendesk)

```bash
curl -s -X POST \
  "https://YOURSUBDOMAIN.zendesk.com/api/services/zis/inbound_webhooks/generic/tsanet_connect" \
  -H "Authorization: Bearer $ZIS_TOKEN" -H "Content-Type: application/json" \
  -d '{"source_system":"tsanet","event_type":"collaboration_event"}'
```

> **Save all three returned values immediately: the ingest URL, the Basic credentials, and the `uuid`.** They are shown only once. There is no list API, so a lost `uuid` is unrecoverable and you will not be able to rotate the credential later. See [ZIS_Rotation_Runbook.md](ZIS_Rotation_Runbook.md).

#### Call 2: register the subscription (to TSANet)

Authenticate with your TSANet API account, the same one the sidebar app uses. Get a token from `POST /v1/login`, then:

```bash
curl -s -X POST "https://connect2.tsanet.net/v1/webhooks" \
  -H "Authorization: Bearer YOUR_TSANET_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "callbackUrl": "THE_INGEST_URL_FROM_CALL_1",
    "callbackAuth": {
      "type": "BASIC",
      "username": "THE_BASIC_USERNAME_FROM_CALL_1",
      "password": "THE_BASIC_PASSWORD_FROM_CALL_1"
    }
  }'
```

`connect2.tsanet.net` is Beta; Production is `connect2.tsanet.org`. Leave `eventTypes` out: omitted means both `collaboration-request.created` and `note.created`, which is what the flow bundle expects.

Save the `id` from the response, which is what you need to inspect or remove the subscription later. The response also carries `secret`, the HMAC signing key, and it is returned only on creation.

> **Use `/v1/webhooks`, not `/v2/webhooks`.** Both endpoints exist and both will accept your subscription, which makes this easy to get wrong and hard to notice. `/v2/` delivers CloudEvents, whose event type strings are prefixed (`org.tsanet.connect.collaboration-request.created`). The flow bundle matches the unprefixed V1 name, so on a `/v2/` subscription every delivery is accepted, returns 200, and then falls through to a no-op: **no ticket is ever created and nothing reports an error.** `/v1/webhooks` is deprecated with a sunset of 2027-01-01; migrating the flow and existing subscriptions to CloudEvents is tracked in `tsanetgit/Zendesk_App#101` and will ship with a future app release. Until then `/v1/` is the correct choice.

> **Inbound push is live.** TSANet to ZIS delivery uses the `callbackAuth` capability (`tsanetgit/Zendesk_App#2`), delivered in Connect API **v3.1.0** and validated on Beta: authenticated deliveries return 200 and create exactly one ticket per case. You can also exercise the pipeline manually by POSTing a `WebhookPayload`-shaped body to the ingest URL with its Basic credentials.

### What happens on each inbound event

One flow handles every TSANet event the same way. TSANet pings the webhook with an event type and case token, ZIS pulls the full case from the TSANet API, then either creates a new ticket (first event on that case) or updates the existing one, syncing TSANet Status, Partner, and Respond By and adding a comment. The ticket is found by searching for the token, so no per-event routing logic is needed. Only the creation event may create a ticket, which is what stops a `note.created` racing ahead and producing a duplicate (`tsanetgit/Zendesk_App#41`).

The created ticket carries the token, status, and partner company, plus, when the collaboration includes submitter contact, a `Submitter: Name <email>` line in the description and opening comment (`tsanetgit/Zendesk_App#57`).

> **Build an INFORMATION alert.** The INFORMATION status is the one most likely to be missed, since it needs a reply before the SLA clock resumes. Add a Zendesk trigger that emails the assigned agent as soon as **TSANet Status** changes to Information. **Admin Center > Objects and rules > Business rules > Triggers.**

### 4e. Forward public replies to the partner (optional, recommended)

By default partner notes flow **into** Zendesk. You can also send agent replies back **out**: when an agent posts a **public reply** on a TSANet ticket, it is forwarded to the partner as a note (`tsanetgit/Zendesk_App#34`). Internal comments are never forwarded, and only replies authored by an agent or admin are sent, never an end customer's.

The flow and its job spec are already deployed (Step 4c installs everything in the bundle). What is left is per-instance wiring:

```bash
# Create the comment-forwarding inbound webhook (its own ingest URL and Basic creds)
curl -s -X POST \
  "https://YOURSUBDOMAIN.zendesk.com/api/services/zis/inbound_webhooks/generic/tsanet_connect" \
  -H "Authorization: Bearer $ZIS_TOKEN" -H "Content-Type: application/json" \
  -d '{"source_system":"zendesk","event_type":"public_comment"}'
```

Then in Admin Center create a **webhook** (Basic auth using the credentials above, endpoint set to its ingest URL, JSON) and a **trigger**. Trigger conditions: comment is public AND current tags include `tsanet_inbound` or `tsanet_outbound`. Trigger action: notify that webhook with the ticket's token, the latest public comment, the ticket ID, and `{{current_user.role}}`. Full request-body detail is in [`zis/README.md`](zis/README.md).

With this in place, the app's **Add Note > Public** simply posts a public reply and lets the trigger deliver it, so the partner receives each note once instead of twice.

---

## Step 5: Test Everything Before Going Live

Before real partner engineers receive your requests and real SLA clocks start, run the full scenario as a fire drill. Run these in order; each builds on the last.

1. **Can you log in to TSANet?** Call the Beta login endpoint with your API credentials and confirm you receive a token.
2. **Can you find a partner?** Search for a test partner by name and confirm you get back a partner ID and department ID.
3. **Can you create a collaboration request?** Submit a test request and confirm you receive a unique token.
4. **Does the ZIS inbound path work?** Send a simulated TSANet event to your ingest URL and confirm the correct ticket is created or updated.
5. **Does the INFORMATION alert work?** Send a simulated INFORMATION event and confirm the TSANet Status field updates and the agent is emailed.
6. **Does the full agent flow work?** Have a real agent open a ticket, click **+ New**, search for a partner, and submit.
7. **Does error handling work?** Submit with an incorrect partner ID and confirm the agent sees a clear, specific error.

When all seven pass, contact TSANet for Production credentials, then update the app's **TSANet environment**, username, and password, and redeploy the bundle so the API host moves with them.

---

## Optional: Native Field Actions and One-Click Macros

Teams that want a no-app fallback can drive the inbound lifecycle from two native Zendesk fields: an agent sets a dropdown and the integration performs the action against TSANet. The flow that does this (`flow_field_action`) is in the same bundle you deployed in Step 4, but it is **off by default** so that a first-time install is not blocked on fields you have not created yet (`tsanetgit/Zendesk_App#132`).

### Create the two fields

In **Admin Center > Objects and rules > Tickets > Fields**:

- **TSANet Action**, a dropdown. The tag values must match exactly:
  - Accept (tag `tsanet_action_accept`)
  - Reject (tag `tsanet_action_reject`)
  - Request Info (tag `tsanet_action_request_info`)
  - Add Note (tag `tsanet_action_add_note`)
- **TSANet Action Text**, a text field for the supporting text (a reject reason, an information question, or a note body).

You do not need to note the IDs. Once both fields exist, run **Detect field IDs** again (Step 3d) and click **Apply** — these two are optional entries in the same detection that filled in the core fields, so it picks them up exactly as it did those.

### Turn the field actions on

Run **Detect field IDs** and **Apply** to fill in the two field IDs, then add your **TSANet engineer email** by hand. That one is not a ticket field, so detection cannot find it. It is the address TSANet records as the engineer on an Accept, and it must be on your member-registered domain.

Then run **Deploy bundle** again. The app re-uploads with the field-action resources included and reinstalls every job spec.

Both field IDs must be set together. One without the other is reported as an error rather than guessed at.

If you are setting this up alongside a first install, create both fields before you reach Step 3d. Detect will pick them up with the rest, and there is nothing to redo.

### Create the optional macros

Macros make the field actions one-click: instead of opening the dropdown, an agent applies a macro that sets the field. Create one per action in **Admin Center > Workspaces > Macros**, or via the API.

| Macro | Sets TSANet Action to |
|---|---|
| TSANet: Accept | `tsanet_action_accept` |
| TSANet: Request Info | `tsanet_action_request_info` |
| TSANet: Reject | `tsanet_action_reject` |
| TSANet: Send partner-only note | `tsanet_action_add_note` |

The agent still types any needed text into TSANet Action Text before applying the macro.

```bash
curl -X POST "https://YOURSUBDOMAIN.zendesk.com/api/v2/macros.json" \
  -H "Authorization: Bearer $SETUP_TOKEN" -H "Content-Type: application/json" \
  -d '{"macro":{"title":"TSANet: Send partner-only note","actions":[{"field":"custom_fields_FIELD_ID","value":"tsanet_action_add_note"}]}}'
```

Macros are per-instance Zendesk configuration and cannot be bundled with the integration, so each account creates its own. The field actions work without macros; the macros are purely a convenience.

---

## What the App Does Day to Day

The sidebar adapts to whether the current ticket is linked to a TSANet collaboration.

**No TSANet case (compact mode):** collapses to a slim 44px bar reading "No active TSANet cases · + New", keeping the sidebar unobtrusive on regular tickets. Clicking **+ New** expands it and opens the New Collaboration dialog.

**With a TSANet case (full panel):**

- All active TSANet collaborations linked to that ticket
- Case status: OPEN, INFORMATION, ACCEPTED, REJECTED, CLOSED
- SLA countdown, colour-coded, on OPEN (unacknowledged) cases only: green above 1 hour, amber 30 to 60 minutes, red under 30 minutes, and BREACHED once the deadline passes
- Partner engineer contact details, once accepted
- Actions: Accept, Reject, Request Info, Respond, Add Note, and Close (outbound only)

**Background, whenever any agent has Zendesk open:**

- Inbound cases are created **server-side by ZIS push**. The sidebar's 1-minute poll is a **fallback** that defers to push, so no duplicate ticket is created while push is working.
- Checks for SLA breaches and adds the `tsanet_sla_breached` tag to overdue tickets.
- Mirrors TSANet collaboration notes into the ticket thread as **internal comments**, labelled by direction: **You** for notes you sent, the partner company for notes received. A note that is your own forwarded public reply is skipped so it does not echo back as a duplicate.

> **SLA scope.** The countdown and breach alerting apply only to the **initial acknowledgment** deadline. Once a case is Accepted, Rejected, or Info Requested, TSANet stops tracking the SLA and the countdown disappears.

> **The panel sizes itself to what is on screen** (app **v1.0.63 or later**). It re-measures when the New Collaboration dialog opens and closes, when a partner's request form renders, when an active case's notes finish loading, and when a case closes. Before v1.0.63 it asked Zendesk for a fixed height regardless of content, so a partner form with six or more fields was cut off at the bottom, search results were clipped, and note threads were truncated mid-note. The height is bounded at both ends, so a very busy ticket carrying several cases with long note threads can still scroll. That is deliberate: the panel must not push your other sidebar apps out of reach.

> **What "Submit failed" means on a new outbound request** (app **v1.0.63 or later**). It means nothing was sent, and retrying is the right response. Creating the case on TSANet and recording it on your own ticket are now reported separately, because the second step writes under Zendesk's safe-update guard and can legitimately refuse when a trigger, an automation, or another agent touches the ticket at the same moment. If that happens the dialog has already closed, and the message tells you the request **was** submitted, names the case token so it can be picked up by hand, and says not to submit again.

### Optional: an SLA breach trigger

Emails the ticket assignee the moment a breach is detected.

**Admin Center > Objects and rules > Business rules > Triggers > Add trigger**, named `TSANet SLA Breach: Notify Assignee`.

Conditions (meet ALL): `Update type` is `Changed`, and `Current tags` includes `tsanet_sla_breached`.
Action: `Notify user` > `(Assignee)`, subject `TSANet SLA Breached: Action Required`, with a body pointing the agent at the TSANet Connect panel to Accept, Reject, or Request More Info.

The tag is added by the ZAF background poller. TSANet also enforces the acknowledgment SLA on its own servers regardless of any Zendesk-side alerting.

---

## Notes: Internal, Partner-only, and Public

A note can reach three different audiences. The app's **Add Note** dialog offers all three as a Visibility choice.

| Add Note > Visibility | Goes to the **partner**? | Visible to the **end customer**? |
|---|---|---|
| **Internal** (default) | No | No, internal Zendesk comment only |
| **Partner only** | **Yes** (TSANet note) | No, surfaces as an internal Zendesk comment for your record |
| **Public** | **Yes** (forwarded as a TSANet note) | Yes, posted as a public reply |

**Partner only** is the middle tier: the partner sees it, the end customer does not. It posts straight to TSANet and writes no public reply, and you still get an internal record on the ticket.

### Zendesk's native reply menu is only Public or Internal

Zendesk's own composer toggle offers just **Public reply** and **Internal note**. That control belongs to Zendesk and **cannot be extended to a third option**. So partner-only is available only through the app's **Add Note** dialog, or, without the app, through the **TSANet Action** field or the **TSANet: Send partner-only note** macro (`tsanetgit/Zendesk_App#56`).

If an agent types directly in the native composer: a **public reply** reaches both the partner (forwarded automatically by the Step 4e trigger) and the customer, and an **internal note** reaches neither.

---

## Updating the App

Zendesk does not support API-based app binary updates.

1. **Admin Center > Apps and integrations > Zendesk Support Apps**
2. Click **TSANet Connect** > **Update**
3. Upload the new ZIP

Settings are preserved across updates, so there is no need to re-enter credentials or re-run Detect.

If the release notes say the ZIS bundle changed, run **Deploy bundle** again afterwards. The embedded bundle and the app's substitution table ship together and are kept identical to `zis/tsanet_connect_bundle.json` by CI, so the two can never drift, but the new bundle still has to be uploaded to take effect.

On **v1.0.63 or later** you do not have to take the release notes' word for it. Open the nav bar screen and read the **Current state** card: the Bundle row compares what is registered on your instance against what the app you just installed would deploy, so it answers the question directly. Most releases do not change the bundle, and a needless deploy is not free, since the upload orphans the installed job specs before the new ones go in.

---

## Authentication Summary

Seven authentication contexts. None of them uses a Zendesk API token.

| Context | Method | Where it is stored |
|---|---|---|
| Setup commands (Steps 1c, 2, 4a, 4d, macros) | Short-lived OAuth setup token (`$SETUP_TOKEN`) | Minted per use, nothing stored |
| ZIS management calls | ZIS OAuth bearer (`$ZIS_TOKEN`) | Used per session, nothing stored |
| Bundle deploy (Step 4c) | Your own signed-in admin session, via the app | Nothing stored. This endpoint refuses OAuth, which is why it runs in the app |
| ZIS to TSANet API (runtime) | OAuth client credentials (Microsoft Entra) | ZIS connection, minted and renewed automatically |
| ZIS to Zendesk API (runtime) | OAuth client credentials against your own instance (`zendesk`, scope `read tickets:write`) | ZIS connection from Step 4a, renewed automatically |
| ZAF app to Zendesk (runtime) | Inherited agent session via the ZAF SDK | Automatic, no credential needed |
| ZAF app to TSANet API (runtime) | TSANet API user login | Password held as a Zendesk secure setting, encrypted, never reaches the front end |

### Audit your connection auth (recommended, repeat before 2027-04-30)

An install whose Zendesk-side ZIS connection still uses an API token loses inbound ticket creation on 2027-04-30 with no warning. [`scripts/audit-connection-auth.py`](scripts/audit-connection-auth.py) checks every connection under the integration and fails loudly if the `zendesk` connection is not OAuth:

```bash
export ZENDESK_SUBDOMAIN=yoursubdomain
export ZIS_TOKEN=...   # from Step 1b
python3 scripts/audit-connection-auth.py
```

Exit codes are cron and CI friendly (0 pass, 1 retirement risk, 2 hygiene warning), so you can schedule it and alert on non-zero. Run it after initial setup, after any credential change, and periodically until the retirement date has passed.

---

## Data Handling

Tickets the integration creates or touches (tagged `tsanet_inbound` or `tsanet_outbound`) carry cross-org case content: partner company names, submitter and engineer contact details, and case narratives in subjects, descriptions, and comments. This content persists until you remove it, and removal applies to your Zendesk copy only. The TSANet platform case and the partner's own CRM copy are separate.

Retention is your responsibility as data controller. The full data map and the two supported removal recipes (whole-ticket deletion or selective redaction) are in [PII_Retention_and_Data_Handling.md](PII_Retention_and_Data_Handling.md).

Those tags are also how the integration's own tickets are identified, so it is worth knowing that **app versions before v1.0.60 could remove them.** See *Recovering tags removed before v1.0.60* under Troubleshooting.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Sidebar shows "Credentials not configured" | Re-check the TSANet username and password in app settings |
| Sidebar says TSANet Connect is not configured yet | Field IDs are not set. Run **Detect field IDs** (Step 3d) |
| Deploy screen is missing from the nav bar | The app is older than v1.0.51. Update it (Step 3a) |
| Accept, or any other action, returns an error | Read the exact message. The app surfaces TSANet's specific reason rather than a generic failure. The most common cause is a domain mismatch: verify the API username is on the domain registered with TSANet, for example `api@yourcompany.com`, not an agent's personal address |
| New Collaboration search returns no results | The partner may not be in TSANet. Check connect.tsanet.org for their membership |
| SLA countdown missing on an OPEN case | `respondBy` may be null. TSANet sets it from your group SLA configuration |
| Background poller not creating tickets | Check the browser console for the `[TSANet BG]` prefix, and confirm credentials are set and TSANet has INBOUND cases |
| A ZIS call returns `401 Authorization failed due to integration mismatch` | Two causes, in order of likelihood. **First, `$ZIS_TOKEN` was minted from the wrong OAuth client.** It must come from `zis_tsanet_connect`, which ZIS created for you in Step 1b; a client you made yourself is refused on every ZIS endpoint. Check with `curl -s -H "Authorization: Bearer $SETUP_TOKEN" "https://YOURSUBDOMAIN.zendesk.com/api/v2/oauth/clients.json" \| jq '.clients[] \| {id, identifier}'` and re-mint from the `zis_tsanet_connect` id. **Second, the container does not exist**, or the name in the URL is capitalized differently. Check with `curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $ZIS_TOKEN" "https://YOURSUBDOMAIN.zendesk.com/api/services/zis/registry/tsanet_connect/job_specs"`, and re-run Step 1b if it is not 200. Neither is a credential problem in the usual sense: an invalid or unset token returns `401 Authentication failed` instead, and a Zendesk API token returns `403 API token is not supported` |
| ZIS reports `AADSTS7000215` but the same secret works elsewhere | The stored value is corrupted (paste artifact, trimmed leading punctuation). Re-send it verbatim via `PATCH /api/services/zis/connections/oauth/clients/tsanet_connect/{uuid}` |
| Updating the OAuth client returns 405 | Use `PATCH`, not `PUT`, on the client endpoint |
| Connection created but no `access_token` | The `verification_code` exchange was skipped. GET the `redirect_url` from the start response with the ZIS bearer |
| A TSANet action fails with a generic 500 in the flow execution log | All TSANet actions send `Accept: application/json, application/problem+json`, so check the actual response body: it carries `title` and `detail` describing the real rejection. TSANet's `/v1` API returns 500 by default for business-rule and authz rejections unless that header is present. This is documented, permanent behaviour, not a bug (`tsanetgit/Connect-API-Code#122`) |
| A flow stopped firing after a redeploy | Check the deploy screen's read-back for stale job specs from an older bundle generation. They still intercept events until uninstalled |
| Tags have gone missing from a TSANet ticket, or **TSANet Status** is blank on a case that is still open | An app older than **v1.0.60** removed them. Update, then see *Recovering tags removed before v1.0.60* below |
| Deploy reports **Integration NOT operational**, naming `jobspec_field_action` as failed to install and then as not installed | The deploy worked. An app older than **v1.0.62** asked Zendesk to install a job spec it had correctly left out of the upload, then checked for it. If `jobspec_handle_ping` and `jobspec_forward_comment` both show `[ok]`, inbound handling and comment forwarding are live. This is the normal state for a first install, because field actions are off until the two optional fields exist. Update to v1.0.62 or later and deploy again |
| An agent saw **Submit failed** on a new outbound collaboration, but the partner received the request anyway | An app older than **v1.0.63** shared one error handler between creating the case and recording it on your ticket, so a failure in the second step reported the whole submit as failed while leaving the dialog filled in. Pressing Submit again opened a **second** request to that partner. Update to v1.0.63 or later, where the dialog closes as soon as the case is created and a bookkeeping failure names the case token and says not to resubmit. To clean up, close the duplicate from the TSANet side |

### Recovering tags removed before v1.0.60

Versions before **v1.0.60** removed a ticket's other tags whenever the app added one of its own. It happened at two moments in ordinary use: the first time a case passed its response deadline and the app added `tsanet_sla_breached`, and every time an agent opened an outbound case from an existing ticket, when the app added `tsanet_outbound`. The cause was Zendesk's tag endpoint, which is named **Add Tags** and replaces the tag set rather than adding to it.

Because Zendesk stores a dropdown field's value as a tag, the **TSANet Status** field was blanked along with the tags.

Updating to v1.0.60 stops it. Updating cannot bring back tags already removed, so this is worth one pass over affected tickets.

**The old tag set is still recorded.** Zendesk keeps the before-and-after of every tag change in the ticket's event history, and that history is not trimmed. Confirmed against tickets whose tags were replaced three weeks earlier: the entry still lists exactly what had been there.

- **Which tickets to check.** Tickets carrying `tsanet_sla_breached` or `tsanet_outbound` that have fewer tags than you would expect, or whose **TSANet Status** is empty on a case that is still open.
- **Where to look.** Open the ticket, switch to the events view, and find the tag change. The entry shows the list that was replaced.
- **If you would rather script it.** `GET /api/v2/tickets/{id}/audits.json` returns events with `field_name: tags`, where `previous_value` holds the replaced list.

---

## Known Limitations

**ZIS scheduled polling is retired.** An earlier design included a ZIS flow (`flow_poll_tsanet`) meant to poll TSANet on a schedule. It never functioned, for three layered reasons: the clock ticket required `new` status, there was no `requestToken` in the automation payload, and the JWT expired during execution. Inbound sync is now ZIS push (primary) with the ZAF background poller as a fallback that defers to push.

**Zendesk Views API cannot set custom field columns.** If you create or modify a view via the API and include `custom_field_XXXXXXX` column IDs in `execution.columns`, the API accepts the request without error and silently reverts to the original columns. Custom field columns on views must be configured by hand in **Admin Center > Workspaces > Views**.

**Mirrored case content persists until you remove it.** See Data Handling above.

---

## Related Documents

| Document | For |
|---|---|
| [`zis/README.md`](zis/README.md) | Full ZIS flow reference: every flow, action, and gotcha |
| [ZIS_Rotation_Runbook.md](ZIS_Rotation_Runbook.md) | Rotating the inbound webhook and OAuth credentials |
| [PII_Retention_and_Data_Handling.md](PII_Retention_and_Data_Handling.md) | What cross-org data lands in your tickets, and how to bound its lifetime |
| [Zendesk_API_Credential_Decision.md](Zendesk_API_Credential_Decision.md) | Why this integration uses no Zendesk API token |
| [ZAF_Custom_Build_Guide.md](ZAF_Custom_Build_Guide.md) | Only for members who cannot install the pre-built ZIP. Not recommended |
| [`zaf-build/README.md`](zaf-build/README.md) | For maintainers: the edit-and-package loop |

---

## Need Help

For credentials, environment access, or installation questions, contact membership@tsanet.org.

Bugs and enhancement requests: https://github.com/tsanetgit/Zendesk_App/issues
