# ZIS Credential Rotation Runbook

How to rotate the three credentials tracked in issue
[#91](https://github.com/tsanetgit/Zendesk_App/issues/91): the **inbound
webhook Basic credential** (the `callbackAuth` credential from
[ZIS_Quick_Start.md](ZIS_Quick_Start.md) Step 5), the **Zendesk OAuth
client secret** behind the `zendesk` connection, and the **TSANet-issued
Entra client secret** behind the TSANet connection (rotated in coordination
with TSANet). Closing sections cover credential handling and
transmission, scheduling, and monitoring for credential abuse.

**When to rotate:**
- On a schedule (quarterly is a reasonable default).
- Immediately on suspected compromise, or when anyone with access to the
  credential leaves your organization.
- Rotating this credential is the primary defense against forged inbound
  events (issue [#90](https://github.com/tsanetgit/Zendesk_App/issues/90)):
  ZIS cannot verify the `X-Hub-Signature-256` HMAC TSANet attaches, so the
  Basic credential is the authenticity control on this pipe.

## What rotation does (and one thing to know)

ZIS cannot regenerate a webhook's credential in place, so rotation is
make-before-break: create a new inbound webhook (new ingest URL, new
credential), re-point the TSANet subscription at it, verify, then delete the
old subscription and the old webhook. **The ingest URL changes on every
rotation.** Deleting the old webhook is what actually revokes the old
credential.

> **Platform notes (validated 2026-07-22):**
> - `DELETE /api/services/zis/inbound_webhooks/generic/{integration}/{uuid}`
>   works (HTTP 204) even though Zendesk's docs do not list a delete
>   operation for inbound webhooks.
> - There is **no list operation**. If you lose a webhook's `uuid`, you
>   cannot enumerate or delete it later. Always record the `uuid` from the
>   create response. Note the create response contains both an `id` (a ULID)
>   and a `uuid`; the API paths accept only the `uuid`.

## Prerequisites

| What | How |
|---|---|
| ZIS OAuth bearer token | Mint per Quick Start Step 3. Scopes must be `zis:read zis:write`. |
| TSANet API bearer token | Entra client-credentials grant, per Quick Start Prerequisites. |
| Old webhook `uuid` | From the create response when the webhook was first set up. |

## Scripted procedure

```bash
export ZENDESK_SUBDOMAIN=yoursubdomain
export ZIS_TOKEN=...            # ZIS OAuth bearer
export TSANET_HOST=connect2.tsanet.org   # or connect2.tsanet.net for Beta
export TSANET_TOKEN=...         # TSANet bearer
export OLD_WEBHOOK_UUID=...     # uuid of the current inbound webhook

# Preview only (read-only, changes nothing):
python3 scripts/rotate-inbound-webhook.py --dry-run

# Rotate:
python3 scripts/rotate-inbound-webhook.py
```

The script:
1. Resolves the old webhook's ingest path from its `uuid`.
2. Finds the TSANet subscription whose `callbackUrl` points at it (pass
   `--subscription-id` if you have more than one candidate).
3. Creates the replacement ZIS webhook and writes the new credentials to
   `zis-webhook-credentials.json` (chmod 600) **before** any cutover.
4. Creates the replacement TSANet subscription (same event types, new URL,
   new `callbackAuth`).
5. Verifies the new path end to end with a synthetic `note.created` delivery
   carrying an unknown token. This is a designed no-op in `flow_handle_ping`
   (the `GuardCreate` state drops it), but it proves ingest auth and wiring.
   Expect one failed flow run in the ZIS Integration Log; that is the probe,
   not a bug.
6. Deletes the old TSANet subscription, then the old ZIS webhook. If either
   delete fails the script says so loudly; follow up manually, because until
   the old webhook is deleted the old credential still works.

On any verification failure the script rolls back (deletes whatever new
halves it created) and leaves the old pipe untouched.

**Afterwards:** store the contents of `zis-webhook-credentials.json` in your
secret manager, record the new `uuid` next to it, and delete the file.

## Legacy installs (webhook `uuid` never recorded)

Installs set up before this runbook existed typically never recorded the
webhook's `uuid`, and ZIS has no list API to recover it. Rotation still
works, with one degradation: pass `--old-ingest-path` (the current ingest
path) instead of `OLD_WEBHOOK_UUID`, and the script skips deleting the old
webhook. The old credential is then **orphaned, not revoked**: nothing
routes to it after the TSANet subscription is deleted, but a party holding
the old credential could still post to the old ingest URL and trigger the
flow. If the old credential may have been exposed, contact Zendesk support
to remove the orphaned webhook. Either way, the rotation converts a legacy
install into a tracked one: from this point on the `uuid` is on record and
every future rotation gets full revocation.

## Overlap window

Between steps 4 and 6 both subscriptions are live and TSANet may deliver each
event twice. This is safe: `flow_handle_ping` deduplicates by `requestToken`
(ticket search plus the `GuardCreate` state), so no duplicate tickets are
created. The window is seconds long under normal operation.

## Manual fallback

If you cannot run the script, the same six steps as curl commands are the
webhook-creation command from [zis/README.md](zis/README.md) (Deploy step 2),
the subscription registration from Quick Start Step 5, and:

```bash
# list subscriptions to find the old id
curl -H "Authorization: Bearer $TSANET_TOKEN" "https://$TSANET_HOST/v1/webhooks"

# delete the old subscription
curl -X DELETE -H "Authorization: Bearer $TSANET_TOKEN" \
  "https://$TSANET_HOST/v1/webhooks/OLD_SUBSCRIPTION_ID"

# delete the old ZIS webhook (revokes the old credential)
curl -X DELETE -H "Authorization: Bearer $ZIS_TOKEN" \
  "https://$ZENDESK_SUBDOMAIN.zendesk.com/api/services/zis/inbound_webhooks/generic/tsanet_connect/OLD_WEBHOOK_UUID"
```

Verify before deleting: send the synthetic delivery from the script's step 5
(a `note.created` body with a made-up `requestToken`, Basic auth with the new
credentials, POSTed to the new ingest URL) and confirm HTTP 200.

## Zendesk OAuth client secret rotation

The `zendesk` ZIS connection (the one that creates and updates tickets) is
backed by a confidential OAuth client on your own Zendesk instance. Rotating
its secret is a different shape from the webhook rotation, because the
platform gives no overlap window:

> **Platform behaviors (validated 2026-07-24):**
> - `PUT /api/v2/oauth/clients/{id}/generate_secret` returns the new secret
>   **in full** (the Admin UI shows regenerated secrets truncated; the API
>   is the reliable path) and invalidates the old secret **immediately**.
> - **Never delete the OAuth client to rotate it.** Deleting a ZIS OAuth
>   client registration cascade-deletes the connections built on it, which
>   destroys the integration. Rotation is regenerate plus update, never
>   delete plus recreate.

```bash
export ZENDESK_SUBDOMAIN=yoursubdomain
export SETUP_TOKEN=...   # admin OAuth bearer (Quick Start Step 1b)
export ZIS_TOKEN=...     # ZIS OAuth bearer

# Preview (read-only):
python3 scripts/rotate-zendesk-oauth-secret.py --dry-run

# Rotate:
python3 scripts/rotate-zendesk-oauth-secret.py
```

The script resolves both sides (the ZIS registration by name, default
`zendesk_self`, and the Zendesk client by its identifier), regenerates the
secret, writes it to a chmod-600 file **before** touching ZIS, PATCHes the
ZIS registration, and verifies the new secret mints tokens.

**The exposure window.** Between the regenerate and the PATCH (fractions of
a second when scripted) ZIS holds a dead secret. This does not interrupt the
integration: the connection's current access token keeps working until its
normal expiry, and by the next renewal ZIS already has the new secret. If
the script dies between the two steps, the saved file has the only copy of
the new secret; recover by re-running just the PATCH (the exact command is
printed on failure). Do not re-run the whole script in that state, as that
would burn another secret.

**Afterwards:** store the new secret wherever your setup-token workflow
keeps it (the same client mints your `SETUP_TOKEN`), then delete the file.

## Entra client secret rotation (coordinated with TSANet)

The TSANet-side connection (`tsanet_oauth` or your named equivalent) is
backed by an Entra client credential that TSANet issues. You cannot
regenerate it yourself, but rotation is still clean because Microsoft Entra
app registrations support **multiple concurrent secrets**: TSANet can add
the new secret while the old one stays valid, which makes this the one
rotation with a true no-pressure overlap window.

1. **Request rotation** from TSANet (membership@tsanet.org). Ask them to
   **add a second secret** to your client rather than replacing the
   existing one, and to deliver it through a secure channel (never email).
2. **Update the ZIS registration.** Find the registration's uuid, then
   PATCH it (PATCH, not PUT, which returns 405). Paste the secret
   **verbatim**: Entra secrets can begin with punctuation, and a trimmed
   paste fails later with `AADSTS7000215`.

   ```bash
   # find the uuid of your Entra registration (name: tsanet_entra by default)
   curl -s -H "Authorization: Bearer $ZIS_TOKEN" \
     "https://YOURSUBDOMAIN.zendesk.com/api/services/zis/connections/oauth/clients/tsanet_connect"

   curl -X PATCH -H "Authorization: Bearer $ZIS_TOKEN" \
     -H "Content-Type: application/json" \
     "https://YOURSUBDOMAIN.zendesk.com/api/services/zis/connections/oauth/clients/tsanet_connect/REGISTRATION_UUID" \
     -d '{"client_secret": "NEW_SECRET_VERBATIM"}'
   ```
3. **Verify** with a direct token mint against Entra (the same check from
   the Quick Start prerequisites, using your `TENANT_ID` and `AUDIENCE`
   with the new secret), and confirm the connection still holds a live
   token on its next renewal.
4. **Confirm to TSANet**, who then deletes the old secret. Rotation is not
   complete until the old secret is removed on the Entra side.

**Expiry is the silent killer.** Entra secrets have a hard expiry (24
months maximum, often shorter). An expired secret stops the ZIS-to-TSANet
leg with `AADSTS7000222` and no earlier warning. Record the expiry date
TSANet gives you and calendar the rotation well before it.

## Handling and transmitting credentials

Rotation only helps if the new credential does not leak on its way to
wherever it is used. Two of the three credentials here arrive from
somewhere else (TSANet issues the Entra secret; the scripts write new
values to a local file), so handling matters as much as cadence.

**Prefer not transmitting a secret at all.** Where a credential can be
created directly in the place it is used, do that instead of moving one
around. For the TSANet connection specifically, ask TSANet whether
certificate credentials or workload identity federation are available for
your account: both replace the shared client secret with an identity proof
that never has to cross an organizational boundary, which removes this
whole class of risk rather than managing it.

**When a secret must be transmitted**, use a one-time-view link (1Password
Send, Bitwarden Send, or an equivalent) with:

* single retrieval, so a second fetch is evidence of interception,
* a short expiry measured in hours, not weeks,
* the link and any passphrase sent over different channels, and
* no copy left in a system of record.

Confirm receipt, verify the link is spent, and where the receiving side
can rotate immediately (the two scripted rotations here), do that on
arrival so the transmitted value is dead within minutes.

**Do not send credentials by email, Slack or Teams message, ticket
comment, SMS, screenshot, or an AI assistant session.** The risk is not
interception in transit, which TLS handles; it is persistence. Those
channels are backed up, indexed, retained, and forwarded, so a secret
pasted once stays readable in several systems long after the person who
sent it has forgotten about it. The same applies to committing a secret to
a repository even briefly: rewriting history does not un-share it.

**Handling the values these scripts produce.** Both rotation scripts write
the new credential to a local file with `0600` permissions rather than
printing it, so it never lands in terminal scrollback or shell history.
Move the contents into your secret manager and delete the file. If your
own tooling stores a copy of the same credential elsewhere, update it in
the same pass; a stale copy is how a rotation ends up half-applied.

**The operating rule.** If a credential is ever handled outside these
paths, treat it as compromised and rotate it rather than judging how
likely exposure was. That rule is only practical when rotation is cheap,
which is what the procedures above are for.

## Scheduling the rotations

All three rotations are safe to run during business hours (each is
designed for zero or near-zero delivery downtime). A reasonable baseline:

| Credential | Cadence | How |
|---|---|---|
| Webhook Basic | Quarterly | cron: `rotate-inbound-webhook.py` |
| Zendesk OAuth secret | Quarterly | cron: `rotate-zendesk-oauth-secret.py` |
| Entra secret | Before expiry, at least annually | Calendar + the coordinated procedure above |
| Connection auth audit | Weekly | cron: `audit-connection-auth.py` (exit non-zero alerts) |

## Monitoring for credential abuse

Rotation bounds a credential's lifetime; monitoring catches misuse inside
it. Two tiers, depending on your Zendesk plan:

**With the Advanced Data Privacy and Protection (ADPP) add-on:** the
Access Log API records per-request activity. Filter it for the
integration's service user and alert on volume spikes, unexpected
endpoints (anything outside ticket read/write), or activity at unusual
hours. This is the control that directly satisfies per-request anomaly
detection; it is plan-gated, and the recipe here is documentation only
(the reference instance does not carry the add-on).

**Without ADPP (baseline for every Enterprise install):** the Audit Log
API records configuration and credential lifecycle events. Poll it on a
schedule for events touching the integration's surface: OAuth client
changes, ZIS connection changes, token grants, and role or password
changes on the service user. Any such event outside a planned rotation
window warrants investigation. As a volume proxy, the integration's
service user should author ticket events at roughly the rate your TSANet
case traffic implies; a scheduled count of its recent ticket updates
(Search API) that deviates sharply from baseline is a cheap, plan-agnostic
tripwire.

## Versioning note

Subscriptions today use the v1 webhook API (`/v1/webhooks`), which is
deprecated with a sunset of 2027-01-01 in favor of `/v2/webhooks`
(CloudEvents payloads). This runbook and script target v1 and will be
revised as part of the v2 migration.
