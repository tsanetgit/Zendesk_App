# Security Considerations

One place for a security reviewer to see every outbound destination this integration talks to, who initiates each call, and with what credential, without reading source. What data is stored where, and for how long, is covered separately in [PII_Retention_and_Data_Handling.md](PII_Retention_and_Data_Handling.md).

Scope: the ZAF sidebar app (including its background poller and the admin deploy screen) and the ZIS flow bundle, as shipped in this repository.

## Outbound destinations

Traffic falls into two origins that matter to an egress reviewer: calls from the agent's or admin's **browser**, and calls made server-side by **Zendesk** on the integration's behalf (the ZAF proxy and ZIS).

| Destination | Initiated from | Credential | What is sent | When |
|---|---|---|---|---|
| `static.zdassets.com` | Agent/admin browser | None | The script request itself, nothing else | Loading the ZAF SDK on each app page. Version-pinned with subresource integrity (SRI), `crossorigin=anonymous` |
| `connect2.tsanet.net` (BETA) / `connect2.tsanet.org` (PRODUCTION) | **Zendesk's ZAF proxy**, server-side. The browser never contacts these hosts directly; proxy mode is also what keeps the API password a secure setting, substituted server-side | TSANet API user (JWT from `POST /v1/login`) | Case data, by design: partner searches, case submissions, accept/reject/request-info, notes, attachments | Sidebar use, and the background poller (every minute while Zendesk is open in a browser) |
| The same two `connect2` hosts | **Zendesk ZIS**, server-side | Microsoft Entra client credential held by the ZIS connection | Inbound event pulls and ticket-driven case actions | On inbound TSANet events and field-driven actions |
| `login.microsoftonline.com` | **Zendesk ZIS**, server-side | Entra client ID + secret | A standard OAuth client-credentials token request. No ticket or case data | When ZIS mints or renews its API token |
| `api.github.com` | **The admin's browser, directly** (the call sets `cors: true`, which bypasses the ZAF proxy) | None. Unauthenticated; the only header is `Accept: application/vnd.github+json` | No ticket, member, or PII data. What is disclosed is the admin's IP address and the fact that the deploy screen was opened | Every open of the deploy screen (the TSANet Connect entry in the left nav bar, an admin surface) |

Notes for allowlist operators:

- From the **agent browser**, the only destinations are your own Zendesk instance, `static.zdassets.com`, and, for admins on the deploy screen only, `api.github.com`.
- The `connect2` hosts and `login.microsoftonline.com` originate from Zendesk's infrastructure, not from your network.
- The `api.github.com` call asks for this repository's latest release so the deploy screen can show whether a newer bundle exists. The response gates nothing: the redeploy recommendation is driven by byte-comparison of bundle content, not by the answer, and the Deploy button never depends on it. If the call fails, or the unauthenticated GitHub rate limit (60 requests per hour per IP) is exhausted, the screen shows an informational row stating explicitly that this does not affect deploying. Blocking `api.github.com` at your egress costs you the release row and nothing else.

## Access control, in one place

Three controls that look alike and do different jobs:

- **Install-time role and group restrictions** (Zendesk-native, per installation) decide who sees the app at all: Admin Center > Apps and integrations > Zendesk Support apps > gear menu on the app > Change settings. Admins and billing admins are two separate roles; select both if both need access. The restriction applies to the whole installation, every location at once: excluding an agent removes the ticket-sidebar panel and the background poller along with the left-nav entry, so use it only for teams that should see no TSANet surface at all. See Quick Start step 3f.
- **Allowed action roles** (app setting) decides which roles can invoke TSANet actions inside a visible panel. Defense-in-depth only.
- **The TSANet Connect API and its credential** are the real authorization boundary. Everything browser-side is UI convenience by comparison.

## Credentials held by the integration

| Credential | Where it lives | Exposure |
|---|---|---|
| TSANet API user password | Zendesk **secure setting** | Encrypted at rest, never readable from front-end JS; the ZAF proxy substitutes it server-side |
| Microsoft Entra client secret | The ZIS connection | Server-side only; never present in the browser |
| Zendesk API token (setup only) | Used from your terminal during Quick Start steps 1 and 4 | Not stored by the app |

## Related documents

- [PII_Retention_and_Data_Handling.md](PII_Retention_and_Data_Handling.md): what data is stored where, retention, and deletion.
- [ZIS_Rotation_Runbook.md](ZIS_Rotation_Runbook.md): rotating the inbound webhook credentials.
- [Zendesk_API_Credential_Decision.md](Zendesk_API_Credential_Decision.md): which Zendesk credential the connector uses, and why.
