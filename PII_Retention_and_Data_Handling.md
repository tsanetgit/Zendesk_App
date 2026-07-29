# PII Retention and Data Handling Guide

**Last updated:** July 2026
**Applies to:** the TSANet Connect Zendesk integration (ZIS bundle + ZAF app)
**Context:** closes the documentation scope of [issue #95](https://github.com/tsanetgit/Zendesk_App/issues/95) (EDB Security Design Review, "Indefinite retention of mirrored PII")

Cross-org case content mirrors into Zendesk tickets and stays there until you
remove it. This guide tells you exactly what data lands where, who controls
each copy, and two supported ways to bound its lifetime: whole-ticket
deletion, or selective PII scrubbing that preserves the case record.

---

## The three-copy model (read this first)

Every collaboration exists as at least three independent copies under three
different controllers:

| Copy | Controller | Removal governed by |
|---|---|---|
| Your Zendesk ticket | **You** (the member) | This guide |
| The TSANet Connect platform case | TSANet | TSANet platform retention (contact TSANet) |
| Your partner's CRM case | The partner member | The partner's own retention policy |

**Anything you delete or scrub applies to your copy only.** Nothing
propagates: the only outbound pipe from your Zendesk to the partner fires on
public comment creation, and the Connect API's case notes are append-only
(no edit or delete endpoint exists). A GDPR erasure obligation that reaches
you covers your copy; reaching the other copies is controller-to-controller
coordination through TSANet, not an API call. If your compliance posture
requires mechanical cross-org erasure, raise it with TSANet as a platform
capability request; it cannot be built at the connector level.

---

## Data map: what the connector writes into your Zendesk

All artifacts below are on tickets tagged `tsanet_inbound` (tickets the
integration created for inbound cases) or `tsanet_outbound` (your own
tickets from which an outbound case was opened). Those tags are the
selectors every recipe in this guide keys on.

| Ticket artifact | Content written | Sensitivity | How to remove (see modes below) |
|---|---|---|---|
| Subject | Partner company name + case summary | Business-confidential; summaries can contain personal data | Rewrite via ticket update (Mode B) or ticket deletion (Mode A) |
| Description (first comment) | Partner company, submitter name and email, priority, case token, full problem description | **Primary PII artifact** | Redaction API (Mode B) or deletion (Mode A) |
| Internal comments | Creation and status-update notices; partner case notes (engineer names, emails, case narrative) | **Primary PII artifact** | Redaction API (Mode B) or deletion (Mode A) |
| Public comments | Partner notes an agent chose to post publicly (visible to your end customer) | Same as above, wider audience | Redaction API (Mode B) or deletion (Mode A) |
| Attachments | Files exchanged on the case (if the attachment workflow is in use) | Varies; treat as PII | Redaction API removes attachments (Mode B) or deletion (Mode A) |
| Custom fields | Case token (opaque), TSANet status (enum), partner company name, respond-by date, TSANet Action / Action Text | Company name is the only sensitive value; token and enums are not personal data | Field clear via ticket update (Mode B, with the audit-history caveat below) or deletion (Mode A) |
| Tags | `tsanet_inbound` / `tsanet_outbound` / `tsanet_updated` / status tags | Not sensitive; these are your retention selectors. Do not remove before the sweep that uses them | n/a |
| Ticket audit history | Previous and new values of every field change, retained indefinitely | Mirrors field-level values (company name at most; person PII lives in comments) | **Ticket deletion only** (see caveat) |

**The audit-history caveat.** Zendesk retains every field change (old and
new value) in the ticket's audit events for the life of the ticket, and
there is no API to redact audit history. Clearing a custom field therefore
hides the value from the ticket but not from the audits endpoint. In
practice this is contained: the connector's custom fields carry the partner
company name at most, while person-level PII (names, emails, narratives)
lives in comments, which redaction removes permanently. If your policy
requires zero residual values including audit events, use Mode A (deletion);
it is the only complete remedy.

**The same retention is what makes tag loss recoverable, and it matters for
these recipes.** App versions before **v1.0.60** removed a ticket's other tags
whenever the app added one of its own, because Zendesk's tag endpoint is named
*Add Tags* and replaces the set (`tsanetgit/Zendesk_App#165`). Two
consequences for this guide:

- **Every recipe here selects on `tsanet_inbound` or `tsanet_outbound`.** A
  ticket that lost those tags is invisible to a sweep that keys on them, so a
  retention pass run against an instance that used an older app can silently
  miss tickets it was meant to cover. Before relying on a tag-scoped sweep for
  a compliance obligation, confirm the instance is on v1.0.60 or later and
  check for tickets carrying only `tsanet_sla_breached`, which is the
  fingerprint of the loss.
- **Recovery of the tags themselves** uses the audit history described above:
  `GET /api/v2/tickets/{id}/audits.json`, events with `field_name: tags`, where
  `previous_value` holds the replaced list. Verified against tickets whose tags
  were replaced three weeks earlier. The property that makes audit history a
  residual-data liability in the paragraph above is the same property that lets
  you restore a selector you need.

---

## Mode A: whole-ticket retention (deletion)

Use when the ticket itself does not need to outlive the retention window.

**Native deletion schedules** (Admin Center > Account > Security > Deletion
schedules) continuously delete qualifying tickets. Two platform facts shape
the recipe:

1. Deletion schedules act on **archived** tickets only, meaning closed for
   more than 120 days. Your effective floor is closure + 120 days plus
   whatever age condition you set.
2. Plan gating: **without** the Advanced Data Privacy and Protection (ADPP)
   add-on you can have one active schedule and the conditions do not
   include tags, so a native schedule cannot target only TSANet tickets.
   **With** ADPP you can run up to 10 schedules with tag conditions.

**Recipe (ADPP):** create a dedicated schedule with conditions
`tags contains tsanet_inbound` (and a second schedule for
`tsanet_outbound` if you also want to bound those), plus
`closed more than N days ago` where N implements your window.

**Recipe (no ADPP):** run a scheduled API sweep instead. Search for
qualifying tickets and bulk-delete them:

```bash
# find TSANet tickets closed longer than your window (example: 180 days)
curl -s "https://YOURSUBDOMAIN.zendesk.com/api/v2/search.json" \
  -G --data-urlencode "query=type:ticket tags:tsanet_inbound status:closed updated<180days" \
  -H "Authorization: Bearer $ZD_TOKEN"

# bulk delete by id (repeat per page of results)
curl -X DELETE "https://YOURSUBDOMAIN.zendesk.com/api/v2/tickets/destroy_many?ids=ID1,ID2,ID3" \
  -H "Authorization: Bearer $ZD_TOKEN"
```

Deleted tickets sit in the deleted-tickets queue for 30 days before
permanent removal; use the permanent-deletion endpoint on that queue if your
window cannot tolerate the extra 30 days. Note for `tsanet_outbound`
tickets: that tag sits on your own support ticket, which may need to outlive
the TSANet content for your own business reasons; for those, prefer Mode B.

## Mode B: selective PII scrub (case record preserved)

Use when the ticket must remain as a business record but the cross-org PII
inside it must be cleaned. Three operations, all keyed on the `tsanet_*`
tag selectors:

1. **Redact PII-bearing comments.** The redaction API permanently removes
   comment text and attachments from Zendesk's databases, search indexes,
   and logs; this is irreversible and is the correct tool for the
   description, note-sync comments, and any public partner notes.
   Connector-written comments are identifiable: creation notices start with
   the inbound-arrow marker, and ZAF note-sync comments carry a
   `tsanet-note-id:` marker line. With ADPP, redaction can additionally be
   suggested automatically or driven by triggers.
2. **Clear PII-bearing fields and the subject.** A ticket update that
   blanks the partner-company field and rewrites the subject (for example
   to `TSANet case <token> (content expired)`) removes the visible values.
   Old values remain in audit history per the caveat above.
3. **Leave the token field and tags in place** until after the sweep runs;
   they are how the sweep finds its targets, and the token is an opaque
   identifier, not personal data.

There is no native scheduler for redaction without ADPP; run it as a
scheduled job against the same search query as the Mode A sweep, redacting
instead of deleting.

## Choosing a window

The retention window is **your policy decision as data controller**,
informed by your obligations under the TSANet partner agreement and your
privacy regime (for example GDPR storage limitation). The connector ships
no default and enforces nothing. Whatever you choose, record it alongside
your other retention policies and treat the recipes above as its
enforcement mechanism.

## Upstream minimization (reducing what lands at all)

The strongest control is writing less PII in the first place. If your
posture requires it, two options exist today: agents can avoid putting
personal data in case summaries and notes (the summary lands in ticket
subjects on both sides), and stricter members can adopt the TSANet
Sovereignty Gateway reference architecture, which classifies and sanitizes
content before it crosses the boundary. A connector-level minimal-mirror
option (for example omitting submitter contact details from ticket bodies)
is a candidate future enhancement; if you want it, open an issue on this
repo.

---

## Verification sources

Zendesk platform behavior cited above was verified July 2026 against:
[Managing deletion schedules](https://support.zendesk.com/hc/en-us/articles/8301879320474),
[Creating ticket deletion schedules](https://support.zendesk.com/hc/en-us/articles/6388012977306),
[Redacting ticket content](https://support.zendesk.com/hc/en-us/articles/4408846470170),
[Redacting identified PII (ADPP)](https://support.zendesk.com/hc/en-us/articles/10474374743450),
[Ticket Audits API](https://developer.zendesk.com/api-reference/ticketing/tickets/ticket_audits/).
Plan gating and the 120-day archive floor can change; re-verify against
current Zendesk documentation before relying on a recipe in a compliance
filing.
