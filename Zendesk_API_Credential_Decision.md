# The `zendesk` connection credential — why it exists, and why it must move to OAuth

**Status:** decision doc
**Scope:** the `zendesk` connection in the ZIS bundle `tsanet_connect`
**Short answer:** The credential itself **cannot be removed** — both ZIS flows run
server-side (triggered by webhooks, with no agent in the browser), and they *must*
authenticate to your Zendesk to create, search, and update tickets. But the **form**
of the credential is changing out from under us: Zendesk is removing API tokens as an
authentication method for the Ticketing API (dates in §4), so the classic-API-token
setup this repo's guides currently describe has a hard end-of-life. New installs
should use an OAuth credential; existing installs must migrate before **April 30,
2027**.

---

## 1. What the credential is actually for

The bundle uses **two** connections, in opposite directions:

| Connection | Direction | Talks to | Credential |
|---|---|---|---|
| `tsanet_oauth` | outbound | TSANet Connect API (`connect2.tsanet.org/v1`) | OAuth2 client credentials, Entra (already in place — see [ZIS_Quick_Start.md](ZIS_Quick_Start.md)) |
| **`zendesk`** | inbound / local | **your own** Zendesk REST API (`/api/v2/...`) | **the Zendesk credential in question** |

The `zendesk` connection is used by **seven** actions across both flows — everything
that reads or writes a ticket:

| Action | Zendesk call | Purpose |
|---|---|---|
| `action_create_ticket` | `POST /api/v2/tickets.json` | create the ticket for a new inbound TSANet request |
| `action_search_ticket` | `GET /api/v2/search.json?query=custom_field_…` | find the ticket by requestToken (dedupe) |
| `action_update_ticket` | `PUT /api/v2/tickets/{id}.json` | update status/fields on a repeat ping |
| `action_zd_get_ticket` | `GET /api/v2/tickets/{id}.json` | read token + note text off the ticket |
| `action_zd_finish_status` | `PUT /api/v2/tickets/{id}.json` | write status comment + field after an action |
| `action_zd_finish_silent` | `PUT /api/v2/tickets/{id}.json` | clear the Action field after "Add Note" |
| `action_zd_finish_fail` | `PUT /api/v2/tickets/{id}.json` | write the failure message + clear the field |

---

## 2. Why it can't just be removed

Both flows are **backend** flows. Neither has a logged-in agent's browser session to
borrow, so neither can call the Zendesk API without its own stored credential.

**Flow A — `flow_handle_ping` (inbound, event source `tsanet`).**
TSANet pings ZIS → ZIS pulls the collaboration request from TSANet → searches your
Zendesk for an existing ticket → **creates or updates** the ticket. This fires when a
*partner* submits a request. There is, by definition, **nobody sitting in Zendesk** at
that moment. The whole value of the flow is "a TSANet request automatically becomes a
Zendesk ticket." Doing that requires a server-side credential — full stop. There is no
browser session to impersonate.

**Flow B — `flow_field_action` (event source `support`, `ticket.CustomFieldChanged`).**
An agent sets the "TSANet Action" field → Support fires a webhook into ZIS → ZIS reads
the ticket, calls TSANet (accept/reject/request-info/note), then writes the result back
onto the ticket. This is *also* a backend webhook flow, so it *also* needs the credential
to read and write the ticket — even though a human kicked it off.

**ZIS has no ambient identity.** Running "inside" your Zendesk account does not let a
ZIS HTTP action call `/api/v2` as the account automatically; every action names a
`connectionName`, and that connection carries credentials. Remove the credential and
all seven ticket operations fail.

---

## 3. The only way to drop it — and what it costs

You could remove the `zendesk` connection **only** by re-architecting so that no
server-side flow touches the Zendesk API:

- **Move Flow B into the sidebar (ZAF) app.** A ZAF app runs in the agent's browser and
  can call `/api/v2/...` using the **agent's own session** (via the framework's
  `client.request`), so *interactive* Accept/Reject/Request-Info/Note could update the
  ticket with **no stored token**. The bundle's Action-field flow is essentially a
  no-app fallback for the same operations. If every agent always uses the sidebar app,
  Flow B's `zendesk` calls could go away.
  - *Cost:* you lose the "just set a field / use a trigger or macro" path. Bulk actions,
    automations, and any non-app workflow stop working. Every action must happen through
    the app UI, live, by a human.

- **Flow A (inbound creation) has no browser-free substitute** that avoids a credential:
  - Email-to-ticket (point TSANet at a support address) creates tickets with no API
    credential — but you lose structured custom-field population and the token↔ticket
    link, and you'd rebuild that with triggers/parsing. Fragile.
  - Creating the ticket from external middleware instead of ZIS just **moves** the
    Zendesk credential to a different box. Same requirement, new home.

**Conclusion:** interactive actions *can* be moved off the credential; **automatic
inbound ticket creation cannot.** As long as that feature stays, a server-side Zendesk
credential stays.

---

## 4. The credential's form: API tokens are being removed

Zendesk is **removing API tokens as an auth method for the Ticketing, Help Center,
and Voice APIs** (announcement linked in §6; scope confirmed 2026-07-07). Every
`/api/v2` call the `zendesk` connection makes is in scope, and the announcement makes
no exception for ZIS connections. Timeline:

- **July 28, 2026** — unused tokens are auto-deactivated, and **new Zendesk accounts
  can no longer create API tokens at all**
- **October 27, 2026** — API token creation is blocked for **every** account
- **April 30, 2027** — **all existing API tokens stop working**; no opt-out or
  extension

So the migration is mandatory, not merely future-proofing, and the onboarding impact
lands before the kill date: a member installing after their creation cutoff cannot
mint an API token in the first place, so setup instructions must be OAuth-based from
the start.

**Migration mechanics (to be validated before the guides change).** Today the guides
register `zendesk` as a `basic_auth` connection holding `email/token` + API token.
The OAuth replacement needs one design decision resolved first: ZIS auto-renews only
`oauth`-type connections (those registered with an OAuth client + token URL), while a
`bearer_token` connection stores a **static** token (see *Gotchas* in
[`zis/README.md`](zis/README.md) — static bearer credentials going stale is exactly
why the basic-auth token was chosen originally). A Zendesk OAuth access token created
under an OAuth client is long-lived unless the account enables token expiry, which
makes a bearer-style registration plausible, but the winning recipe must be proven
against a live instance before the Quick Start and README prerequisites are rewritten.

---

## 5. Keep it on a dedicated user — regardless of token vs. OAuth

Either mechanism *acts as a Zendesk user*, and every ticket create/comment/field-change
is **attributed to that user**. Use a **dedicated service user** (e.g. "TSANet Connect
Integration"), not a real person's account, because:

1. **Attribution / audit clarity** — automated activity shows as the service user, not
   as a staff member who "did" hundreds of things they never touched.
2. **Continuity** — the credential dies with its user. Tie it to an employee and
   offboarding silently breaks inbound ticket creation. A service user survives.
3. **Blast radius / rotation** — you can deactivate the service user (killing the
   integration) without disturbing anyone's real access.
4. **Permissions** — the user must be an **admin** (creating tickets, searching,
   writing custom fields). Note this typically consumes a **paid agent seat** — a real
   cost to weigh, but the right trade for a clean audit trail.

---

## 6. Recommendation

- **Keep the Zendesk credential.** It is load-bearing for automatic inbound ticket
  creation, which is the point of the integration.
- **Put it on a dedicated admin service user**, not a personal account.
- **Migrate the `zendesk` connection to OAuth.** No longer optional: token creation is
  blocked for new Zendesk accounts July 28, 2026 and for all accounts October 27, 2026,
  and existing tokens die April 30, 2027. New installs should not create an API token
  at all.
- **Optionally** reduce the credential's footprint by steering agents to the sidebar
  app for case actions — but treat that as a UX/hardening choice, not a way to delete
  the credential.

### Checking what your instance uses today

The ZIS connections endpoint requires a **ZIS OAuth bearer token** (API-token basic
auth returns 403 "API token is not supported"). Mint one per
[ZIS_Quick_Start.md](ZIS_Quick_Start.md) Step 4, then:

```bash
curl -s "https://YOURSUBDOMAIN.zendesk.com/api/services/zis/integrations/tsanet_connect/connections/all" \
  -H "Authorization: Bearer $ZIS_TOKEN" | python3 -m json.tool
```

The response groups connections into arrays keyed by type (`basic_auth`, `oauth`, …);
read which group the `zendesk` entry appears under. This returns metadata only — never
the stored secret. (TSANet's reference test instance was confirmed `basic_auth` on
2026-07-07, i.e. still on the API token, as this repo's guides currently instruct.)

---

## Appendix — sources

- Bundle: [`zis/tsanet_connect_bundle.json`](zis/tsanet_connect_bundle.json)
- [Announcing the removal of API tokens as an authentication method for API requests | Zendesk](https://support.zendesk.com/hc/en-us/articles/10840968198042-Announcing-the-removal-of-API-tokens-as-an-authentication-method-for-API-requests)
- [Understanding connections | Zendesk Developer Docs](https://developer.zendesk.com/documentation/integration-services/developer-guide/understanding-connections/)
- [All Connections (list) | Zendesk Developer Docs](https://developer.zendesk.com/api-reference/integration-services/connections/all_connections/)
- [Bearer Token Connections](https://developer.zendesk.com/api-reference/integration-services/connections/bearer_token_connections/)
- [OAuth Connections](https://developer.zendesk.com/api-reference/integration-services/connections/oauth_connections/)
- [Making API requests on behalf of end users](https://developer.zendesk.com/documentation/ticketing/using-the-zendesk-api/making-api-requests-on-behalf-of-end-users/)
