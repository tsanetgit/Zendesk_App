# ZIS Inbound Webhook Credential Rotation Runbook

How to rotate the Basic credential that secures the TSANet -> ZIS inbound
webhook (the `callbackAuth` credential from [ZIS_Quick_Start.md](ZIS_Quick_Start.md)
Step 5). This is one of the three credentials tracked in issue
[#91](https://github.com/tsanetgit/Zendesk_App/issues/91); the Zendesk OAuth
client secret and the TSANet-issued Entra client secret have their own
procedures (tracked in #91, not yet in this runbook).

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

## Versioning note

Subscriptions today use the v1 webhook API (`/v1/webhooks`), which is
deprecated with a sunset of 2027-01-01 in favor of `/v2/webhooks`
(CloudEvents payloads). This runbook and script target v1 and will be
revised as part of the v2 migration.
