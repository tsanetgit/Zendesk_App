/* TSANet Connect — admin-triggered ZIS bundle deploy (Zendesk_App#120).
 *
 * Why this exists
 * ---------------
 * `POST /api/services/zis/registry/{integration}/bundles` rejects OAuth. It
 * accepts an API token or an authenticated admin session. Zendesk is removing
 * API tokens (none for accounts created on/after 2026-07-28, none for anyone
 * after 2026-10-27, all deactivated 2027-04-30) and password auth for APIs is
 * already gone. So the documented curl install stops working for new members,
 * and this screen is the replacement: ZAF `client.request()` runs on the
 * signed-in admin's own session, which is the same mechanism Admin Center uses
 * against ZIS, and needs no credential the admin has to create or maintain.
 *
 * Deliberate design points
 * ------------------------
 * - The bundle is EMBEDDED (assets/tsanet_connect_bundle.json) rather than
 *   fetched. The substitution table below is code; shipping the two together
 *   makes a bundle-vs-code mismatch structurally impossible. CI enforces that
 *   the embedded copy matches zis/tsanet_connect_bundle.json.
 * - Uploading a bundle ORPHANS the currently-installed job specs, so a failed
 *   deploy can leave a previously-working instance worse off. Every step is
 *   therefore idempotent, job spec installs are all attempted rather than
 *   aborting on the first failure, and success is judged by READING BACK the
 *   registry, never by trusting the POST responses.
 * - Expected-installed is derived from the SUBSTITUTED bundle that was actually
 *   uploaded, not from the embedded source. Those diverge whenever field actions
 *   are off. A job spec that is
 *   installed but absent from the bundle is a stale orphan from an older
 *   generation and still intercepts events (see zis/README.md), so it is
 *   surfaced as a warning instead of being ignored or silently removed.
 * - nav_bar is visible to every agent, and this project has not found an
 *   admin-only ZAF location. The role check below is therefore a UX gate, not a
 *   security boundary. The boundary is server-side, and it was probed rather
 *   than taken on the vendor's word (#125, closed 2026-07-28): a Staff
 *   custom-role session — the most privileged non-admin role on that instance,
 *   so a refusal there implies every lesser role — got 403 from both
 *   POST /api/services/zis/registry/tsanet_connect/bundles ("Only admin user
 *   is allowed") and PUT /api/v2/apps/installations/{id}. The two enforce
 *   independently, so one proving out did not imply the other. Both probes
 *   sent a deliberately malformed body, and the explicit authorization message
 *   distinguishes a refusal from a CSRF failure.
 *
 *   Scoped honestly: one role, one instance, one day. It establishes that the
 *   server refuses, not that it will refuse forever — a platform change would
 *   need a fresh probe, which is why the finding names the date.
 */
/* global ZAFClient */

// Surface anything that escapes, before the app has a chance to render nothing.
// A ZAF app lives in a cross-origin iframe, so a thrown error or a rejected
// promise in here is invisible from the parent page's console — the screen just
// stays blank, which is indistinguishable from "still loading". That cost real
// debugging time twice, so failures now show up on the screen they broke.
(function () {
  function surface(what, msg) {
    var g = document.getElementById('gate');
    if (!g) { return; }
    g.textContent = what + ': ' + msg;
    g.classList.remove('hidden');
  }
  window.addEventListener('error', function (e) {
    surface('Script error', (e.message || 'unknown') +
            (e.lineno ? ' (line ' + e.lineno + ')' : ''));
  });
  window.addEventListener('unhandledrejection', function (e) {
    var r = e && e.reason;
    surface('Unhandled rejection', (r && (r.message || r.statusText)) || String(r));
  });
})();

(function () {
  'use strict';

  var client = ZAFClient.init();

  // Per-instance since #174. It was fixed until a member's account refused to
  // register it at all: POST /registry/tsanet_connect returned 400 "the
  // integration: tsanet_connect is not available for upsert by this account" on
  // a clean Enterprise account with full admin, while a different name
  // registered cleanly. Zendesk documents this: "the name you choose has to be
  // globally unique and can be up to 64 characters long" — globally across every
  // Zendesk account, not per account — so the name cannot be a constant we
  // choose on every member's behalf.
  //
  // It stays the DEFAULT, and it is also the placeholder the shipped bundle
  // carries — exactly like tsanet_oauth. A synthetic __INTEGRATION__ token would
  // break the embedded-copy check and the documented curl path for the accounts
  // where this name still works.
  var DEFAULT_INTEGRATION = 'tsanet_connect';
  var ARN_PREFIX = 'zis:' + DEFAULT_INTEGRATION + ':';

  // These were module-level constants, evaluated before settings exist. They are
  // functions now because the name they are built from arrives with the
  // settings, not with the file.
  function integrationName(s) {
    return ((s && s.tsanet_integration_name) || '').trim() || DEFAULT_INTEGRATION;
  }
  // encodeURIComponent is a no-op for the documented charset (all URL-unreserved).
  // It is here because pre-flight records an invalid name and then keeps going, so a
  // name carrying / ? or % would otherwise reach a request URL after being rejected.
  function registryPath(s) { return '/api/services/zis/registry/' + encodeURIComponent(integrationName(s)); }
  function jobSpecPrefix(s) { return 'zis:' + integrationName(s) + ':job_spec:'; }

  // Bundle placeholder -> app setting holding the real value.
  var FIELD_PLACEHOLDERS = {
    '1234567890': 'field_id_token',
    '1234567891': 'field_id_action',
    '1234567892': 'field_id_status',
    '1234567893': 'field_id_partner',
    '1234567894': 'field_id_respond_by',
    '1234567895': 'field_id_action_text',
    // Not a field id: the shared partner user (#178). Same substitution
    // mechanism, but OPTIONAL — when blank, stripSharedAuthor removes the
    // author_id keys instead, and every connector comment keeps today's
    // attribution (the authenticated connection user).
    '1234567896': 'shared_author_user_id'
  };

  var HOST_PLACEHOLDER = 'connect2.tsanet.org';   // bundle ships Production
  var CONN_PLACEHOLDER = 'tsanet_oauth';
  var EMAIL_PLACEHOLDER = 'YOUR_TSANET_API_EMAIL';
  var AUTO_ACCEPT_PLACEHOLDER = 'AUTO_ACCEPT_MODE';
  // Also the shipped bundle's literal value, which is what makes default-value
  // substitution a byte-identical no-op and keeps the curl path shipping sane text.
  var NEXT_STEPS_DEFAULT = 'Accepted via Zendesk.';

  // Where the app's own releases are published (#171). Public and unauthenticated, so
  // this is read with cors:true — see checkLatestRelease. A repo rename would not break
  // it: the GitHub API 301-redirects renamed repositories.
  var GITHUB_LATEST_RELEASE =
    'https://api.github.com/repos/tsanetgit/Zendesk_App/releases/latest';

  // "Optional: Native Field Actions and One-Click Macros" in the Installation
  // Guide. The TSANet Action / Action Text ticket fields are created AFTER the
  // bundle is deployed — the guide's optional section sits ~110 lines below the
  // deploy step — so a first-time installer does not have them yet. Requiring
  // them made the deploy screen unreachable on a fresh install (#132). The ZAF
  // sidebar app never reads either field: it calls /approval, /rejection,
  // /information and /notes directly. This set is only for the field-driven
  // path, which the docs title "no ZAF app required".
  //
  // These resources are owned EXCLUSIVELY by flow_field_action, so dropping them
  // leaves the rest of the bundle intact. Exclusivity is the membership test, and
  // it has been violated silently once: action_zd_get_ticket joined this list
  // when only flow_field_action's GetTicket used it, then #208 gave
  // flow_handle_ping a ShowTicket state calling the same action, and the strip
  // kept deleting it — breaking the update path on every field-actions-off
  // install (#219 review found it). action_ts_note was deliberately never here
  // (flow_forward_comment uses it), and since #219 the accept path
  // (action_ts_accept, action_zd_finish_status, action_zd_finish_fail) is
  // referenced by flow_handle_ping's auto-accept states — which exist in the
  // bundle text whether or not the setting is on — so those three cannot be
  // stripped either.
  var FIELD_ACTION_RESOURCES = [
    'flow_field_action',
    'jobspec_field_action',
    'action_ts_reject',
    'action_ts_info',
    'action_zd_finish_note_receipt'
  ];

  // Settings that only exist to serve FIELD_ACTION_RESOURCES.
  // tsanet_engineer_email left this list with #219: YOUR_TSANET_API_EMAIL lives
  // in action_ts_accept, which the auto-accept states keep in every bundle, so
  // the email substitutes on every deploy (falling back to tsanet_username,
  // which is domain-valid by the same rule and required by the manifest).
  var FIELD_ACTION_SETTINGS = ['field_id_action', 'field_id_action_text'];

  // on | off | partial. `partial` is an error rather than a guess: the two field
  // ids are a functional pair, and picking a side would either half-wire the
  // flow or silently discard a value the admin entered on purpose.
  function fieldActionMode(s) {
    var a = (s.field_id_action || '').toString().trim();
    var t = (s.field_id_action_text || '').toString().trim();
    if (a && t) { return 'on'; }
    if (!a && !t) { return 'off'; }
    return 'partial';
  }

  // Remove the field-action resources from the bundle text. Parses and
  // re-serialises rather than editing the string: these are whole JSON objects,
  // and a textual cut would have to get brace matching and trailing commas right
  // on every one of them.
  function stripFieldActions(text) {
    var b = JSON.parse(text);
    var dropped = [];
    FIELD_ACTION_RESOURCES.forEach(function (name) {
      if (b.resources && Object.prototype.hasOwnProperty.call(b.resources, name)) {
        delete b.resources[name];
        dropped.push(name);
      }
    });
    return { text: JSON.stringify(b, null, 2), dropped: dropped };
  }

  // With field actions off, the retained finish actions (action_zd_finish_status
  // and action_zd_finish_fail stay in every bundle since #219 — the auto-accept
  // states reference them) still carry the "clear the TSANet Action field" write.
  // That field does not exist on a field-actions-off instance, so its placeholder
  // has no value, survives substitution, and fails the deploy as a leftover
  // (caught by the #219 config-matrix test). Remove exactly that custom_fields
  // entry; structural for the same reason as the other strips. An emptied
  // custom_fields array is deleted outright — a ticket update with only a
  // comment is valid, an empty array is noise.
  function stripActionFieldClears(text) {
    var b = JSON.parse(text);
    var stripped = [];
    Object.keys(b.resources || {}).forEach(function (name) {
      var def = ((b.resources[name].properties || {}).definition || {});
      var ticket = ((def.requestBody || {}).ticket) || {};
      if (!ticket.custom_fields) { return; }
      var before = ticket.custom_fields.length;
      ticket.custom_fields = ticket.custom_fields.filter(function (f) {
        return String(f.id) !== '1234567891';
      });
      if (ticket.custom_fields.length !== before) { stripped.push(name); }
      if (!ticket.custom_fields.length) { delete ticket.custom_fields; }
    });
    return { text: JSON.stringify(b, null, 2), stripped: stripped };
  }

  // With no shared author configured (#178), remove the author_id keys rather
  // than leaving a placeholder for the leftovers check to trip on. Structural
  // for the same reason as stripFieldActions: a textual cut would have to get
  // the comma right in every comment object.
  function stripSharedAuthor(text) {
    var b = JSON.parse(text);
    var stripped = [];
    Object.keys(b.resources || {}).forEach(function (name) {
      var def = ((b.resources[name].properties || {}).definition || {});
      var comment = ((def.requestBody || {}).ticket || {}).comment;
      if (comment && Object.prototype.hasOwnProperty.call(comment, 'author_id')) {
        delete comment.author_id;
        stripped.push(name);
      }
    });
    return { text: JSON.stringify(b, null, 2), stripped: stripped };
  }

  // ------------------------------------------------------------ field detect
  //
  // Admins were copying five to seven numeric ids out of Admin Center URLs into
  // app settings (#135). The app can read the same fields itself, so it does.
  //
  // Matching is by title, which is fragile in a specific way: titles drift. The
  // published guides already disagree — GitBook says "TSANet Tokens (Multi)",
  // the repo Quick Start says "TSANet Tokens Multi" — so an exact compare would
  // already miss that field for half our readers. Hence normalise, and hence the
  // rule that nothing is written without the admin seeing the mapping first: a
  // bad paste fails loudly at pre-flight, a bad auto-match would not.
  var EXPECTED_FIELDS = [
    { key: 'field_id_token',        title: 'TSANet Token',        type: 'text',   required: true  },
    { key: 'field_id_status',       title: 'TSANet Status',       type: 'tagger', required: true  },
    { key: 'field_id_partner',      title: 'TSANet Partner',      type: 'text',   required: true  },
    { key: 'field_id_respond_by',   title: 'TSANet Respond By',   type: 'date',   required: true  },
    { key: 'field_id_tokens_multi', title: 'TSANet Tokens Multi', type: 'text',   required: false },
    { key: 'field_id_action',       title: 'TSANet Action',       type: 'tagger', required: false },
    { key: 'field_id_action_text',  title: 'TSANet Action Text',  type: 'text',   required: false }
  ];

  // Case and punctuation are noise; word order and spelling are not.
  function normTitle(t) {
    return (t || '').toString().toLowerCase().replace(/[^a-z0-9]+/g, '');
  }

  var state = { settings: null, bundleText: null, bundle: null, steps: [],
                preflightOk: false, detected: null, installationId: null,
                appId: null, appVersion: null, latestTag: null, bundleCheckedAt: null };

  // Rows of the Current state card, keyed by lane so each resolves independently.
  var STATUS_LABEL = { bundle: 'Bundle', app: 'App version', release: 'Latest release' };
  var status = {
    bundle:  { ok: null, name: 'Bundle: checking…' },
    app:     { ok: null, name: 'App version: checking…' },
    release: { ok: null, name: 'Latest release: checking…' }
  };

  // ---------------------------------------------------------------- helpers

  function el(id) { return document.getElementById(id); }

  function show(id, on) { el(id).classList[on ? 'remove' : 'add']('hidden'); }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function resize() {
    try { client.invoke('resize', { width: '100%', height: (document.body.scrollHeight + 40) + 'px' }); }
    catch (e) { /* nav_bar may not honour resize; harmless */ }
  }

  // client.request rejects with an object carrying status/responseText. Normalise
  // both outcomes so callers never branch on shape.
  function req(opts) {
    return client.request(opts).then(
      function (data) { return { ok: true, status: 200, data: data }; },
      function (e) {
        var body = '';
        try { body = e.responseText || JSON.stringify(e.responseJSON || {}); } catch (x) { body = String(e); }
        return { ok: false, status: (e && e.status) || 0, body: String(body).slice(0, 500) };
      }
    );
  }

  function record(name, ok, detail) {
    state.steps.push({ name: name, ok: ok, detail: detail || '' });
    return ok;
  }

  // ------------------------------------------------------------ substitution

  // Substitution is textual, not structural, because the field-id placeholders
  // appear in two contexts: as unquoted JSON numbers (`{"id": 1234567890`) and
  // inside jq expression strings (`select(.id==1234567890)`). Parsing and
  // walking the object would still need text replacement inside string leaves,
  // so it buys nothing.
  //
  // Textual substitution means a setting value can escape its JSON context.
  // Probed on the shipped bundle (#124): `tsanet_engineer_email` set to
  // `a@b.com", "evil": "1` broke out of the string, injected a key into
  // action_ts_accept, AND still parsed as valid JSON — so nothing downstream
  // rejected it. Three layers below, because none alone is sufficient:
  //   1. validate  — reject values that could break out at all
  //   2. escape    — JSON-escape what is interpolated into string positions
  //   3. parse     — refuse to upload anything that is not valid JSON
  // Layer 1 currently rejects exactly the characters layer 2 would escape, so
  // layer 2 is redundant *today* — deliberately kept so that loosening layer 1
  // later cannot silently reintroduce the injection. Layer 3 does NOT catch the
  // vector above on its own (the exploit parses cleanly); it turns the remaining
  // malformed cases, such as a non-numeric field id, from fail-open to
  // fail-closed.
  //
  // The host is not user input (it is derived from tsanet_env and both values
  // are literals in this file), so it needs no validation.

  // Effective connection name. Trim BEFORE defaulting: defaulting first lets a
  // whitespace-only setting survive `||` (it is truthy) and then trim to "",
  // which uploads an empty connection name that every TSANet action silently
  // fails to resolve through its Catch. Lived in three places and was wrong in
  // two of them, so it is computed once here.
  function connName(s) {
    return (s.tsanet_connection_name || '').trim() || CONN_PLACEHOLDER;
  }

  // Escape for interpolation into a JSON string literal.
  function jsonStr(v) {
    var q = JSON.stringify(String(v));
    return q.slice(1, q.length - 1);
  }

  function substitute(text, s) {
    var out = text;
    var missing = [];
    var invalid = [];

    // The integration name runs FIRST, before every other pass, for two reasons.
    //
    // It cannot run last: the connection pass below documents "nothing
    // substitutes after this point", and a connection name of
    // "zis:tsanet_connect:" is legal under that pass's validation, so an
    // integration pass after it would rewrite the value that was supposed to
    // ship. That is #127 from the other side.
    //
    // The charset is Zendesk's, matched exactly rather than guessed at:
    // "Integration names support the following characters: lower-case letters
    // (a-z), numbers, hyphens (-), and underscores (_)" and "can be up to 64
    // characters long". Being stricter than that would refuse a name ZIS accepts,
    // and the refusal would come from us with no way for the member to tell.
    //
    // The charset is NOT what closes the substitution hazard, though. Running
    // first means later passes scan text containing the inserted name, and a name
    // of `1234567890` is charset-legal, lands as `zis:1234567890:` — where the
    // colons are word boundaries — so the field-id pass's \b alternation matches
    // it and rewrites our integration name into a field id. A name merely
    // CONTAINING a token does the same (`edb-1234567890`). The token exclusion
    // below is the load-bearing check; it reads FIELD_PLACEHOLDERS directly, so a
    // placeholder added later is covered without anyone remembering to come here.
    var integration = integrationName(s);
    if (!/^[a-z0-9_-]{1,64}$/.test(integration)) {
      invalid.push('tsanet_integration_name must be 1-64 characters of lowercase letters, digits, ' +
                   'underscore or hyphen — the set Zendesk documents for ZIS integration names. ' +
                   'A name carrying a colon would silently re-segment every ZIS resource ARN while ' +
                   'staying valid JSON, so it is rejected here');
    } else if (Object.keys(FIELD_PLACEHOLDERS).some(function (ph) { return integration.indexOf(ph) !== -1; })) {
      invalid.push('tsanet_integration_name must not contain a field-id placeholder token (' +
                   Object.keys(FIELD_PLACEHOLDERS).join(', ') + '); the field-id pass would rewrite it');
    } else if (integration !== DEFAULT_INTEGRATION) {
      // Top-level bundle name: structural. A textual match on
      // `"name": "tsanet_connect"` cannot prove that no resource ever carries the
      // same key and value; setting the parsed property can. The file already
      // parses and re-stringifies here (stripFieldActions), so this costs nothing
      // new.
      // Drift guard, on the INPUT rather than the output. This pass knows two
      // shapes: the top-level name, and the ARN prefix. If the bundle ever gains
      // a THIRD place carrying the bare name — an `event_source`, say — renaming
      // would rewrite the two it knows, deploy clean, install every spec, and
      // then receive nothing, because the webhook emits under the new name while
      // the spec still listens on the old one. Silent total failure, on exactly
      // the accounts this setting exists for, and invisible to an end-to-end run
      // under the default name.
      //
      // Counting the shipped bundle's own occurrences cannot false-positive on a
      // member's chosen name (a name like `my-tsanet_connect` would defeat a
      // \btsanet_connect\b check on the output). Today: 18 ARNs + 1 name = 19.
      var totalOld = (out.match(/tsanet_connect/g) || []).length;
      var arnOld = (out.match(/zis:tsanet_connect:/g) || []).length;
      if (totalOld !== arnOld + 1) {
        invalid.push('the embedded bundle carries the default integration name in ' +
                     (totalOld - arnOld - 1) + ' place(s) this substitution does not know about. ' +
                     'Renaming would rewrite only some of them, so nothing is uploaded');
      } else {
        try {
          var parsed = JSON.parse(out);
          parsed.name = integration;
          out = JSON.stringify(parsed, null, 2);
          // The 18 resource ARNs: textual, anchored on the trailing colon. Nothing
          // else in the bundle starts with `zis:tsanet_connect:`, and split/join
          // never rescans inserted text. Inside the try so a parse failure cannot
          // leave `out` half-substituted for the passes that follow.
          out = out.split(ARN_PREFIX).join('zis:' + integration + ':');
        } catch (e) {
          invalid.push('embedded bundle is not valid JSON: ' + String(e.message || e).slice(0, 120));
        }
      }
    }

    // Decide the field-action mode first: it determines which resources ship and
    // therefore which settings are required at all.
    var mode = fieldActionMode(s);
    var dropped = [];
    if (mode === 'partial') {
      invalid.push('field_id_action and field_id_action_text must both be set or both be empty — ' +
                   'they are a pair, and one alone cannot drive a field action');
    } else if (mode === 'off') {
      var strip = stripFieldActions(out);
      out = strip.text;
      dropped = strip.dropped;
      out = stripActionFieldClears(out).text;
    }
    // Which settings count as required this run.
    var optional = {};
    if (mode !== 'on') { FIELD_ACTION_SETTINGS.forEach(function (k) { optional[k] = true; }); }

    // Shared author (#178) is optional in BOTH modes: set, its id substitutes
    // like a field id; blank, the author_id keys are stripped so the bundle
    // carries no placeholder and every comment keeps today's attribution. A
    // non-numeric value still fails below rather than being stripped — a typo
    // is a mistake to surface, not a request to turn the feature off.
    optional.shared_author_user_id = true;
    var sharedAuthorStripped = [];
    if (!(s.shared_author_user_id || '').toString().trim()) {
      var authorStrip = stripSharedAuthor(out);
      out = authorStrip.text;
      sharedAuthorStripped = authorStrip.stripped;
    }

    // Validate every field id BEFORE substituting anything, then replace all six
    // in a SINGLE pass. Replacing them one at a time re-scans text already
    // written: a value equal to another placeholder's token was expanded again by
    // the next iteration, so field_id_token=1234567891 silently emitted
    // field_id_action's value instead (#127). It passed digits-only validation,
    // left no placeholder behind, and parsed — all three #124 layers missed it.
    // One alternation with a callback replaces each token exactly once and never
    // rescans inserted text.
    var fieldValues = {};
    Object.keys(FIELD_PLACEHOLDERS).forEach(function (ph) {
      var key = FIELD_PLACEHOLDERS[ph];
      var val = (s[key] || '').toString().trim();
      // With field actions off, these placeholders are gone from `out` along with
      // the resources that held them, so a blank value is correct, not missing.
      if (!val) { if (!optional[key]) { missing.push(key); } return; }
      // These ids land in an unquoted numeric position. Anything non-numeric
      // either corrupts the JSON or injects structure, so refuse it here.
      if (!/^\d+$/.test(val)) {
        invalid.push(key + ' must be digits only (a numeric Zendesk id)');
        return;
      }
      fieldValues[ph] = val;
    });
    if (!missing.length && !invalid.length) {
      // \b so a placeholder can never match inside a longer numeric id.
      out = out.replace(
        new RegExp('\\b(' + Object.keys(FIELD_PLACEHOLDERS).join('|') + ')\\b', 'g'),
        // A token with no value can only mean an optional placeholder survived a
        // strip that should have removed it. Return it unchanged so the leftovers
        // check below fails the deploy, rather than writing "undefined" into the
        // bundle — which would be valid JSON pointing at nothing.
        function (whole, token) {
          return Object.prototype.hasOwnProperty.call(fieldValues, token) ? fieldValues[token] : token;
        }
      );
    }

    var host = (s.tsanet_env === 'PRODUCTION') ? 'connect2.tsanet.org' : 'connect2.tsanet.net';
    out = out.split(HOST_PLACEHOLDER).join(host);

    // action_ts_accept is in every bundle since #219 (the auto-accept states
    // reference it), so this placeholder always needs a value. Blank falls back
    // to tsanet_username: the API user's email, domain-valid by the same TSANet
    // rule (main.js uses it the same way for submitterContactDetails), and a
    // required app setting — so `missing` fires only when BOTH are blank, which
    // means credentials are absent and the deploy has bigger problems.
    var email = (s.tsanet_engineer_email || '').trim() || (s.tsanet_username || '').trim();
    if (!email) {
      missing.push('tsanet_engineer_email (or tsanet_username, its fallback)');
    } else if (/["\\\u0000-\u001f]/.test(email)) {
      invalid.push('tsanet_engineer_email contains a quote, backslash or control character');
    } else {
      out = out.split(EMAIL_PLACEHOLDER).join(jsonStr(email));
    }

    // Auto-accept (#219). The mode token substitutes INSIDE BuildSubmitter's jq
    // expression string, where it becomes the flow's $.submitter.auto_accept.
    // AUTO_ACCEPT_MODE is uppercase, so the integration-name charset
    // ([a-z0-9_-]) cannot collide with it, and non-numeric, so the field-id \b
    // alternation cannot touch it. Polarity is fail-safe on purpose: on the
    // documented curl path the token is unsubstituted, and an unsubstituted
    // literal !== "on" evaluates to off. Never invert the Choice in the bundle.
    var autoOn = String(s.tsanet_auto_accept == null ? '' : s.tsanet_auto_accept).trim().toLowerCase() === 'true';
    out = out.split(AUTO_ACCEPT_PLACEHOLDER).join(autoOn ? 'on' : 'off');

    // The acceptance text is key-anchored like connectionName (the default is a
    // natural-language string, and a bare split/join would rewrite an identical
    // phrase anywhere it appeared), replaced via function for the same $$ hazard.
    // Trim-then-default: a cleared setting must fall back, not substitute "",
    // because an empty nextSteps is a value TSANet may reject and no member wants.
    var nextSteps = (s.tsanet_auto_accept_next_steps || '').trim() || NEXT_STEPS_DEFAULT;
    if (/["\\\u0000-\u001f]/.test(nextSteps)) {
      invalid.push('tsanet_auto_accept_next_steps contains a quote, backslash or control character');
    } else {
      out = out.replace(
        new RegExp('("nextSteps"\\s*:\\s*)"' + reEsc(NEXT_STEPS_DEFAULT) + '"', 'g'),
        function (whole, prefix) { return prefix + '"' + jsonStr(nextSteps) + '"'; }
      );
    }

    // Only the TSANet connection is per-instance. The Zendesk-side connection is
    // named "zendesk" and is fixed, so match the key/value pair rather than the
    // bare string to avoid collateral edits.
    //
    // This runs LAST on purpose. It used to run before the email pass, so a
    // connection name of "YOUR_TSANET_API_EMAIL" was overwritten by the email
    // that followed it (#127). Nothing substitutes after this point, so the
    // value written here is the value that ships.
    var conn = connName(s);
    if (/["\\\u0000-\u001f]/.test(conn)) {
      invalid.push('tsanet_connection_name contains a quote, backslash or control character');
    } else {
      // Function replacement, not a replacement string: in a string `$` is
      // special, so a connection name containing `$$` would silently become `$`
      // (valid JSON, wrong name, past all three layers) and `$1`/`$&` would
      // splice the match back in. A function's return value is used verbatim.
      out = out.replace(
        new RegExp('("connectionName"\\s*:\\s*)"' + CONN_PLACEHOLDER + '"', 'g'),
        function (whole, prefix) { return prefix + '"' + jsonStr(conn) + '"'; }
      );
    }

    var leftovers = [];
    Object.keys(FIELD_PLACEHOLDERS).forEach(function (ph) {
      if (new RegExp('\\b' + ph + '\\b').test(out)) { leftovers.push(ph); }
    });
    if (out.indexOf(EMAIL_PLACEHOLDER) !== -1) { leftovers.push(EMAIL_PLACEHOLDER); }
    if (out.indexOf(AUTO_ACCEPT_PLACEHOLDER) !== -1) { leftovers.push(AUTO_ACCEPT_PLACEHOLDER); }
    // A renamed instance must carry no trace of the default name. If one survives,
    // the bundle gained an ARN shape this pass does not know about, and half the
    // resources would deploy under a registry the other half does not reference.
    // Fail closed like every other placeholder rather than upload a split bundle.
    if (integration !== DEFAULT_INTEGRATION && out.indexOf(ARN_PREFIX) !== -1) {
      leftovers.push(ARN_PREFIX);
    }

    // Nothing malformed leaves the browser.
    var parseError = '';
    if (!missing.length && !invalid.length && !leftovers.length) {
      try { JSON.parse(out); }
      catch (e) { parseError = String(e.message || e).slice(0, 200); }
    }

    return { text: out, missing: missing, invalid: invalid,
             leftovers: leftovers, parseError: parseError,
             mode: mode, dropped: dropped };
  }

  function bundleJobSpecNames(bundle) {
    var names = [];
    var res = (bundle && bundle.resources) || {};
    Object.keys(res).forEach(function (k) {
      if (res[k] && res[k].type === 'ZIS::JobSpec') { names.push(k); }
    });
    return names.sort();
  }

  // Resolve every expected field against this instance. Returns one row per
  // expected field so the admin sees the whole picture, including what was not
  // found, rather than only the successes.
  function detectFields() {
    el('detect-btn').disabled = true;
    el('detect-note').textContent = ' looking up ticket fields…';
    return req({ url: '/api/v2/ticket_fields.json?page[size]=100', type: 'GET' }).then(function (r) {
      if (!r.ok) {
        el('detect-note').textContent = '';
        el('detect-btn').disabled = false;
        renderSteps('detect-rows', [{ ok: false, name: 'Could not read ticket fields',
                                      detail: 'HTTP ' + r.status + ' ' + r.body }]);
        return;
      }
      var live = (r.data && r.data.ticket_fields) || [];
      var rows = [], resolved = {}, blocked = false;

      EXPECTED_FIELDS.forEach(function (f) {
        var hits = live.filter(function (lf) { return normTitle(lf.title) === normTitle(f.title); });
        var current = (state.settings[f.key] || '').toString().trim();

        if (hits.length > 1) {
          // Never pick. Two fields answering to one name is a question only the
          // admin can settle, and guessing wrong is silent.
          blocked = true;
          rows.push({ ok: false, name: f.title + ' — ambiguous',
                      detail: hits.length + ' fields share this name (ids ' +
                              hits.map(function (h) { return h.id; }).join(', ') +
                              '). Rename or delete the duplicate, then Detect again.' });
          return;
        }
        if (!hits.length) {
          rows.push({ ok: f.required ? false : null,
                      name: f.title + ' — not found' + (f.required ? '' : ' (optional)'),
                      detail: f.required
                        ? 'Required. Create it in Admin Center > Objects and rules > Tickets > Fields.'
                        : 'Not created on this instance, which is fine — the feature it belongs to stays off.' });
          if (f.required) { blocked = true; }
          return;
        }

        var hit = hits[0];
        // A title match on the wrong type is the strongest available signal that
        // this is a different field that happens to share a name.
        if (hit.type !== f.type) {
          blocked = true;
          rows.push({ ok: false, name: f.title + ' — wrong type',
                      detail: 'Found id ' + hit.id + ' but it is a "' + hit.type +
                              '", expected "' + f.type + '". Refusing to use it.' });
          return;
        }

        resolved[f.key] = String(hit.id);
        var same = current === String(hit.id);
        rows.push({ ok: true,
                    name: f.title + ' → ' + hit.id,
                    detail: same ? 'Already set to this in app settings.'
                                 : (current ? 'App settings currently say ' + current + ' — Apply will change it.'
                                            : 'Not set in app settings yet — Apply will set it.') });
      });

      state.detected = blocked ? null : resolved;
      renderSteps('detect-rows', rows);
      el('detect-btn').disabled = false;
      el('detect-note').textContent = '';
      var changes = state.detected && Object.keys(resolved).filter(function (k) {
        return (state.settings[k] || '').toString().trim() !== resolved[k];
      });
      if (state.detected && changes.length) {
        show('apply-btn', true);
        el('apply-btn').textContent = 'Apply to app settings (' + changes.length + ' change' +
                                      (changes.length === 1 ? '' : 's') + ')';
      } else {
        show('apply-btn', false);
        if (state.detected) { el('detect-note').textContent = ' app settings already match'; }
      }
      resize();
    });
  }

  // Write the resolved ids into this app's own installation settings. Sends only
  // the resolved keys: the installations endpoint merges, so everything else —
  // including the secure tsanet_password, which is never readable here — is left
  // untouched. Verified by diffing a full settings snapshot before and after.
  function applyDetected() {
    if (!state.detected || !state.installationId) { return; }
    el('apply-btn').disabled = true;
    el('detect-note').textContent = ' saving…';
    return req({
      url: '/api/v2/apps/installations/' + state.installationId + '.json',
      type: 'PUT', contentType: 'application/json',
      data: JSON.stringify({ settings: state.detected })
    }).then(function (r) {
      el('apply-btn').disabled = false;
      if (!r.ok) {
        el('detect-note').textContent = ' failed: HTTP ' + r.status + ' ' + r.body;
        return;
      }
      // Update the in-memory copy so pre-flight reflects the save without a reload.
      Object.keys(state.detected).forEach(function (k) { state.settings[k] = state.detected[k]; });
      el('detect-note').textContent = ' saved';
      show('apply-btn', false);
      // Apply changes the settings substitute() runs on, so it changes what "what this
      // app would deploy" means, which is half of the bundle comparison. Same hazard as
      // a deploy, so the same two-part treatment: invalidate HERE, on the line after the
      // settings actually changed, rather than refreshing at the end of a chain that can
      // stop early. detectFields() and preflight() are both allowed to reject, and a
      // rejection there would otherwise leave a verdict computed against settings that
      // no longer exist — a stale "matches what this app would deploy" surviving a state
      // change, which is the case the deploy path treats as the dangerous one.
      status.bundle = { ok: null, name: 'Bundle: checking…',
                        detail: 'App settings just changed, so the previous result no longer applies.' };
      renderStatus();
      // Re-checked on BOTH outcomes for the same reason. guard() swallows its own
      // errors, so this cannot turn a pre-flight failure into an unhandled rejection.
      var recheck = function () { return guard('bundle', checkBundle); };
      return detectFields()
        .then(function () { return preflight(); })
        .then(recheck, recheck);
    });
  }

  // ------------------------------------------------------------- current state
  //
  // #171. This screen used to say nothing about what was already on the instance, so
  // the only way to find out whether a deploy was needed was to deploy. That is a bad
  // default: an upload orphans the installed job specs before the new ones go in, so a
  // needless deploy is a small outage (hence the warn banner above the button).
  //
  // The recommendation is driven by BUNDLE CONTENT, never by a version number. Between
  // v1.0.54 and v1.0.60 there were six releases and the bundle did not change once, so
  // a version compare would have told every member to redeploy six times for nothing.
  // The app and release rows are reference only and never gate the Deploy button.
  //
  // Comparing content is exact rather than approximate. `substitute()` reproduces the
  // running bundle byte-for-byte once key order is canonicalised: probed against a test
  // instance, all 18 resources matched. So this needs no version stamp inside the
  // bundle, and it works on instances deployed long before this code shipped.

  // Order-insensitive serialisation. ZIS was observed not to reorder what it stores,
  // but nothing promises key order survives a round trip, so never compare raw JSON
  // text.
  function canon(v) {
    if (v === null || typeof v !== 'object') { return JSON.stringify(v); }
    if (Object.prototype.toString.call(v) === '[object Array]') {
      return '[' + v.map(canon).join(',') + ']';
    }
    return '{' + Object.keys(v).sort().map(function (k) {
      return JSON.stringify(k) + ':' + canon(v[k]);
    }).join(',') + '}';
  }

  var RE_META = /[.*+?^${}()|[\]\\]/g;
  function reEsc(s) { return String(s).replace(RE_META, '\\$&'); }

  // Every SUBSTRING site substitute() rewrites, paired with the shape its output can
  // take. hasPlaceholder and shapePattern both walk this one table, so a site cannot be
  // known to substitute() and unknown to the comparison.
  //
  // That divergence is not hypothetical. #175 added the ARN prefix to substitute() and
  // the comparison never learned it, so on a renamed instance all 18 resource ARNs
  // failed a literal compare and the card told a member whose bundle was in sync to
  // redeploy — the exact false positive this card exists to prevent, on the one row
  // that drives the recommendation. Two independent lists are what made that possible.
  //
  // connectionName is deliberately NOT here: shapeEq matches it structurally, as a bare
  // leaf, rather than by wildcarding a substring. That seam is covered by the self-check
  // in compareBundle, which fails loudly on any site this vocabulary has fallen behind
  // on, leaf-shaped ones included.
  var SUBST_SITES = Object.keys(FIELD_PLACEHOLDERS).map(function (ph) {
    return { token: ph, shape: '\\d+' };
  }).concat([
    { token: HOST_PLACEHOLDER,  shape: 'connect2\\.tsanet\\.(?:org|net)' },
    { token: EMAIL_PLACEHOLDER, shape: '[^"]*' },
    // Lives inside BuildSubmitter's jq expression string; the deployed value is
    // exactly on or off, so the shape says so rather than wildcarding wider.
    { token: AUTO_ACCEPT_PLACEHOLDER, shape: '(?:on|off)' },
    // The nextSteps site is the default TEXT, not an uppercase token: the deployed
    // value is whatever the member typed, so the shape is any string content.
    { token: NEXT_STEPS_DEFAULT, shape: '[^"]*' },
    // The live side's integration name was validated against /^[a-z0-9_-]{1,64}$/ before
    // it was deployed, so match that charset rather than re-validating it here. It
    // cannot match a colon, so it never over-runs into the ARN's later segments.
    { token: ARN_PREFIX,        shape: 'zis:[a-z0-9_-]{1,64}:' }
  ]);

  function hasPlaceholder(s) {
    for (var i = 0; i < SUBST_SITES.length; i++) {
      if (s.indexOf(SUBST_SITES[i].token) !== -1) { return true; }
    }
    return false;
  }

  // A pristine string leaf becomes a matcher with each substitution site wildcarded.
  // Needed because placeholders also live inside a jq expression string
  // (flow_field_action's Extract step), which no structural walk can reach.
  function shapePattern(s) {
    // The text being split is already reEsc'd, so each token has to be reEsc'd to match
    // it. The old code did that for the host and not for the other two, which was
    // correct only because those tokens carry no regex metacharacters — a property of
    // today's values, not of the code. Uniform now, so it stays true.
    var out = reEsc(s);
    SUBST_SITES.forEach(function (site) {
      out = out.split(reEsc(site.token)).join(site.shape);
    });
    return new RegExp('^' + out + '$');
  }

  // Does the running bundle have the same SHAPE as the pristine one — same keys, same
  // definitions, with only the per-instance substitution sites allowed to differ?
  //
  // This deliberately takes no settings. Reverse-substituting the running side back to
  // placeholders was the first design and it is unsound: that needs the values that were
  // live AT DEPLOY TIME, and in the settings-drift case those are exactly what we do not
  // have. It would report every settings change as a new bundle generation. Wildcarding
  // the pristine side instead means the shape answer holds even on an instance whose
  // settings are missing or malformed.
  function shapeEq(pris, live) {
    if (pris === null || typeof pris !== 'object') {
      if (typeof pris === 'number') {
        // A field-id placeholder stands for whatever id this instance uses. Every other
        // number is part of the definition and must match exactly.
        if (Object.prototype.hasOwnProperty.call(FIELD_PLACEHOLDERS, String(pris))) {
          return typeof live === 'number';
        }
        return pris === live;
      }
      if (typeof pris === 'string') {
        // connectionName is the only per-instance bare string, and `tsanet_oauth`
        // occurs nowhere else in the bundle (checked), so this needs no key context.
        if (pris === CONN_PLACEHOLDER) { return typeof live === 'string'; }
        if (hasPlaceholder(pris)) {
          return typeof live === 'string' && shapePattern(pris).test(live);
        }
      }
      return pris === live;
    }
    if (live === null || typeof live !== 'object') { return false; }
    var pArr = Object.prototype.toString.call(pris) === '[object Array]';
    if (pArr !== (Object.prototype.toString.call(live) === '[object Array]')) { return false; }
    if (pArr) {
      if (pris.length !== live.length) { return false; }
      for (var i = 0; i < pris.length; i++) {
        if (!shapeEq(pris[i], live[i])) { return false; }
      }
      return true;
    }
    var pk = Object.keys(pris).sort(), lk = Object.keys(live).sort();
    if (pk.length !== lk.length) { return false; }
    for (var j = 0; j < pk.length; j++) {
      // Element-wise, not a joined compare: any separator can itself occur inside a
      // JSON key. The first attempt here joined on a NUL byte, which made `file`
      // report this asset as binary and made grep skip it silently by default.
      if (pk[j] !== lk[j]) { return false; }
      if (!shapeEq(pris[pk[j]], live[pk[j]])) { return false; }
    }
    return true;
  }

  // Classify one running bundle against what this app would deploy right now.
  // Returns { verdict, detail } where verdict is one of:
  //   insync | settings | mode | generation | shapeonly
  function compareBundle(runningRes) {
    var sub = substitute(state.bundleText, state.settings);
    var pristineRes = (state.bundle && state.bundle.resources) || {};

    // Shared-author mode (#178): with the setting blank, substitute() strips the
    // author_id keys, so the pristine side must be normalized the same way or
    // the self-check reads our own output as shape drift inside every
    // comment-writing action. One-sided on purpose: a LIVE bundle still carrying
    // author keys after the setting was cleared must NOT normalize away — that
    // difference is real, and the "differs" verdict's advice (redeploy) is
    // exactly what applies the strip.
    if (!(state.settings.shared_author_user_id || '').toString().trim()) {
      pristineRes = JSON.parse(
        stripSharedAuthor(JSON.stringify({ resources: pristineRes })).text
      ).resources;
    }

    // Same normalization for the action-field clears (#219): with field actions
    // off, substitute() removes the retained finish actions' clear-the-Action-
    // field entry, so the pristine side must drop it too or the self-check reads
    // our own output as shape drift on action_zd_finish_status / _fail and every
    // field-actions-off install sees the "cannot verify its own bundle" banner.
    // One-sided for the same reason as shared-author: a LIVE bundle still
    // carrying the clear entry after field actions were turned off is real
    // drift, and the 'settings' verdict's advice (redeploy) is what removes it.
    if (fieldActionMode(state.settings) === 'off') {
      pristineRes = JSON.parse(
        stripActionFieldClears(JSON.stringify({ resources: pristineRes })).text
      ).resources;
    }

    // Self-check, before any verdict about the member's instance. substitute()'s output
    // is a bundle this comparison MUST recognise, because we generated it one line ago
    // from the pristine side it is compared against. If it does not, substitute() has a
    // per-instance site that SUBST_SITES and shapeEq do not know.
    //
    // This is the control the SUBST_SITES table cannot be: the table stops the two
    // comparator helpers drifting from each other, but #175 was substitute() drifting
    // from BOTH of them, and substitute()'s passes are too heterogeneous to drive off
    // the same table. So assert the property instead of duplicating the list. A
    // divergence that used to surface as confident wrong advice on a member's screen
    // now surfaces here, on our own output, as a visible app bug.
    var selfBad = '';
    try {
      var subRes = (JSON.parse(sub.text) || {}).resources || {};
      Object.keys(subRes).forEach(function (n) {
        if (selfBad) { return; }
        // substitute() only ever REMOVES resources (the field-action strip); no pass
        // adds one. So a name here the pristine side lacks means a pass rewrote a
        // resource KEY rather than a value. The field-id and ARN passes run over the
        // bundle TEXT, so nothing structurally confines them to values, and comparing
        // only the names both sides carry would skip exactly that case in silence.
        if (!Object.prototype.hasOwnProperty.call(pristineRes, n)) {
          selfBad = 'it renamed the resource ' + n;
          return;
        }
        // The reverse direction — a pristine name missing from subRes — is the
        // field-action strip doing its job, and the mode classification below reports
        // it. Not a vocabulary failure.
        if (!shapeEq(pristineRes[n], subRes[n])) { selfBad = n; }
      });
    } catch (e) {
      // A parse failure here is not the member's problem either: deploy() refuses to
      // upload unparseable output, so this is still an app-side fault.
      selfBad = 'the bundle this app generated did not parse';
    }
    if (selfBad) {
      return { verdict: 'selfcheck', detail: 'This app version cannot verify its own bundle (' +
               selfBad + '), so it will not guess what is deployed. That is a bug in the app, ' +
               'not a problem with your instance — please report it to TSANet. Pre-flight and ' +
               'Deploy are unaffected.' };
    }

    var expected = Object.keys(pristineRes);
    if (sub.mode === 'off') {
      expected = expected.filter(function (n) { return FIELD_ACTION_RESOURCES.indexOf(n) === -1; });
    }
    var running = Object.keys(runningRes);

    // 1. resource set. A difference confined to the field-action resources is a mode
    //    change, not a new generation — reporting "newer bundle available" for someone
    //    who simply cleared two field ids would be alarming and wrong. The pre-flight
    //    downgrade guard already covers the orphan consequence at deploy time.
    var delta = expected.filter(function (n) { return running.indexOf(n) === -1; })
      .concat(running.filter(function (n) { return expected.indexOf(n) === -1; }));
    var modeOnly = delta.length > 0 &&
      delta.every(function (n) { return FIELD_ACTION_RESOURCES.indexOf(n) !== -1; });
    if (delta.length && !modeOnly) {
      return { verdict: 'generation', detail: 'Resource set differs: ' + delta.sort().join(', ') };
    }

    // 2. shape, over the resources both sides carry. This runs even when the only set
    //    difference is the field-action mode, because a bundle can be BOTH an older
    //    generation AND a mode change. Returning 'mode' before checking shape would
    //    print "not a new bundle" without having established it.
    var common = expected.filter(function (n) { return running.indexOf(n) !== -1; });
    var badShape = common.filter(function (n) { return !shapeEq(pristineRes[n], runningRes[n]); });
    if (badShape.length) {
      return { verdict: 'generation', detail: 'Definitions differ: ' + badShape.join(', ') };
    }
    if (modeOnly) {
      return { verdict: 'mode', detail: 'Field-action resources differ: ' + delta.sort().join(', ') +
               '. Everything both sides carry is otherwise identical, so this is the optional ' +
               'field-driven feature going on or off, not a new bundle.' };
    }

    // 3. values. Only meaningful when the substitution is clean; otherwise the shape
    //    answer above is all that can honestly be claimed.
    if (sub.missing.length || sub.invalid.length || sub.leftovers.length || sub.parseError) {
      return { verdict: 'shapeonly',
               detail: 'The deployed bundle is the generation this app ships. Whether its ' +
                       'per-instance values are current cannot be checked until app settings ' +
                       'are complete — see Pre-flight below.' };
    }
    var subRes = JSON.parse(sub.text).resources;
    var badVal = common.filter(function (n) { return canon(subRes[n]) !== canon(runningRes[n]); });
    if (badVal.length) {
      return { verdict: 'settings', detail: 'Same bundle generation, but the values baked into it ' +
               'no longer match current app settings: ' + badVal.join(', ') +
               '. Deploy to apply the current settings.' };
    }
    return { verdict: 'insync', detail: common.length + ' of ' + common.length +
             ' resources match what this app would deploy.' };
  }

  // "matches what this app would deploy" and deliberately NOT "no deploy needed": this
  // compares the REGISTERED BUNDLE'S CONTENT and establishes nothing about whether its
  // job specs are installed. A deploy interrupted between the upload and the job-spec
  // installs — the orphaning the warn banner above the button describes — leaves the
  // registry matching while the integration processes no events at all. Claiming "no
  // deploy needed" there would be actively wrong in the one failure this card exists to
  // catch. Installed-state lives in Pre-flight and in the post-deploy verify.
  var BUNDLE_ROW = {
    insync:     { ok: true,  name: 'Bundle: matches what this app would deploy' },
    settings:   { ok: false, name: 'Bundle: app settings changed since it was deployed' },
    mode:       { ok: null,  name: 'Bundle: field-action mode differs from app settings' },
    // States the fact, and nothing more. shapeEq establishes that the two sides DIFFER;
    // it never establishes which is newer. The previous wording ("older than the one
    // this app ships — deploy recommended") asserted a direction it could not know, and
    // an app BEHIND the deployed bundle is reachable — this repo ships custom builds,
    // which is why the release row already tolerates an app ahead of the published tag.
    // Telling that admin to deploy would downgrade the bundle and orphan its job specs,
    // the outage this card exists to prevent. The detail line names the resources.
    generation: { ok: false, name: 'Bundle: differs from what this app would deploy' },
    shapeonly:  { ok: null,  name: 'Bundle: right generation, values unverified' },
    selfcheck:  { ok: null,  name: 'Bundle: cannot be checked by this app version' }
  };

  // GET /bundles lists metadata only (uuid, name, description, zis_template_version —
  // no timestamp and no version), so the content comes from GET /bundles/{uuid}, which
  // returns the bundle unwrapped with `resources` at the top level.
  function checkBundle() {
    return req({ url: registryPath(state.settings) + '/bundles', type: 'GET' }).then(function (r) {
      if (!r.ok) {
        // A fresh install has no integration yet, so this 404s. That is a real answer,
        // not a failure, and the screen never said it before.
        status.bundle = (r.status === 404)
          ? { ok: null, name: 'Bundle: not deployed yet',
              detail: 'This instance has no TSANet bundle. Deploy to install it.' }
          : { ok: null, name: 'Bundle: could not check',
              detail: 'HTTP ' + r.status + ' ' + r.body };
        return;
      }
      var list = (r.data && r.data.bundles) || [];
      if (!list.length) {
        status.bundle = { ok: null, name: 'Bundle: not deployed yet',
                          detail: 'The integration exists but carries no bundle. Deploy to install it.' };
        return;
      }
      // zis/README.md records that an upload REPLACES the installed bundle, and a test
      // instance that has been redeployed carries exactly one. Rather than depend on
      // that, treat "in sync" as "any registered bundle matches" and say how many are
      // registered — correct whether upload replaces or appends.
      var cap = 5;
      var read = list.slice(0, cap);
      var best = null;
      return read.reduce(function (chain, b) {
        return chain.then(function () {
          if (best && best.verdict === 'insync') { return; }   // stop at the first match
          return req({ url: registryPath(state.settings) + '/bundles/' + encodeURIComponent(b.uuid), type: 'GET' })
            .then(function (r2) {
              if (!r2.ok) {
                if (!best) {
                  best = { verdict: null, detail: 'Could not read the deployed bundle: HTTP ' +
                                                  r2.status + ' ' + r2.body };
                }
                return;
              }
              var res = (r2.data && r2.data.resources) || {};
              var v = compareBundle(res);
              // `!best.verdict` matters: a failed read above parks a verdict-less
              // placeholder in `best`, and without this a later bundle's definitive
              // answer would be discarded in favour of "could not check".
              if (!best || !best.verdict || v.verdict === 'insync') { best = v; }
            });
        });
      }, Promise.resolve()).then(function () {
        if (!best || !best.verdict) {
          status.bundle = { ok: null, name: 'Bundle: could not check',
                            detail: (best && best.detail) || 'No readable bundle.' };
          return;
        }
        var row = BUNDLE_ROW[best.verdict];
        var extra = (list.length > 1)
          ? ' ' + list.length + ' bundles are registered for this integration' +
            (list.length > cap ? ', of which the first ' + cap + ' were checked' : '') + '.'
          : '';
        status.bundle = { ok: row.ok, name: row.name, detail: best.detail + extra };
      });
    });
  }

  // The installed version comes from the platform, never from a constant in this file:
  // a hardcoded copy drifts the moment a release does not touch this file. Keyed on the
  // app id from metadata() and never on the app's name — the registered name on the
  // instance this was probed against ("TSANet Connect ZAF") is not the manifest name, so
  // a name match would silently miss. The response is unwrapped, with `version` on top.
  function checkAppVersion() {
    if (!state.appId) {
      status.app = { ok: null, name: 'App version: unavailable',
                     detail: 'Zendesk supplied no app id for this installation.' };
      return Promise.resolve();
    }
    return req({ url: '/api/v2/apps/' + encodeURIComponent(state.appId) + '.json', type: 'GET' })
      .then(function (r) {
        var v = r.ok && r.data && r.data.version;
        if (!v) {
          status.app = { ok: null, name: 'App version: could not read',
                         detail: r.ok ? 'No version on the app record.' : ('HTTP ' + r.status + ' ' + r.body) };
          return;
        }
        state.appVersion = String(v);
        status.app = { ok: true, name: 'App version: ' + state.appVersion + ' installed here' };
      });
  }

  // Compare dot-separated numeric versions. A lexical compare would call 1.0.9 newer
  // than 1.0.60, and this project ships both one- and two-digit patch numbers. Returns
  // null when either side does not parse, so the row can decline to make a claim rather
  // than guess a direction.
  function cmpVersion(a, b) {
    var pa = String(a).split('.'), pb = String(b).split('.');
    var n = Math.max(pa.length, pb.length);
    for (var i = 0; i < n; i++) {
      var na = parseInt(pa[i] || '0', 10), nb = parseInt(pb[i] || '0', 10);
      if (isNaN(na) || isNaN(nb)) { return null; }
      if (na !== nb) { return na < nb ? -1 : 1; }
    }
    return 0;
  }

  // cors:true runs this in the admin's own browser instead of the Zendesk proxy. Two
  // reasons: no domainWhitelist entry is needed (that property governs secure settings,
  // and nothing secret goes to GitHub), and GitHub's unauthenticated 60/hour limit then
  // applies per admin rather than to a shared Zendesk egress address every customer
  // would be sharing. Elsewhere in this app proxy mode is required precisely because a
  // secure setting is involved; that reason does not apply here.
  //
  // Advisory only. Offline, corporate egress block, 403 rate limit, changed payload:
  // every failure leaves this row as an honest "unknown" and none of them touches the
  // Deploy button.
  function checkLatestRelease() {
    return req({ url: GITHUB_LATEST_RELEASE, type: 'GET', cors: true,
                 headers: { Accept: 'application/vnd.github+json' } }).then(function (r) {
      var tag = r.ok && r.data && r.data.tag_name;
      if (!tag) {
        status.release = { ok: null, name: 'Latest release: could not check',
                           detail: 'GitHub was not reachable from this browser' +
                                   (r.ok ? '.' : ' (HTTP ' + r.status + ').') +
                                   ' This is informational only and does not affect deploying.' };
        return;
      }
      state.latestTag = String(tag);
      var link = (r.data && r.data.html_url) ? ' ' + r.data.html_url : '';
      if (!state.appVersion) {
        status.release = { ok: null, name: 'Latest release: ' + state.latestTag,
                           detail: link.replace(/^ /, '') };
        return;
      }
      var c = cmpVersion(state.appVersion, String(tag).replace(/^v/, ''));
      if (c === 0) {
        status.release = { ok: true, name: 'Latest release: ' + state.latestTag + ' — this app is current' };
      } else if (c === -1) {
        status.release = { ok: null, name: 'Latest release: ' + state.latestTag + ' — a newer app is available',
                           detail: 'Installed here: ' + state.appVersion + '. Upgrading the app is separate ' +
                                   'from deploying the bundle, and the Bundle row above is what says whether ' +
                                   'a deploy is needed.' + link };
      } else if (c === 1) {
        // Ahead of the published latest. A custom build (see ZAF_Custom_Build_Guide.md)
        // legitimately lands here, and telling that admin to "upgrade" would be wrong.
        status.release = { ok: null, name: 'Latest release: ' + state.latestTag,
                           detail: 'This instance runs ' + state.appVersion +
                                   ', which is not behind the latest published release.' + link };
      } else {
        // cmpVersion returned null: one side does not parse as a dotted numeric version.
        // Report both and claim NO direction — any ordering here would be invented.
        status.release = { ok: null, name: 'Latest release: ' + state.latestTag,
                           detail: 'Installed here: ' + state.appVersion +
                                   '. These cannot be compared automatically.' + link };
      }
    });
  }

  function renderStatus() {
    renderSteps('status-rows', [status.bundle, status.app, status.release]);
  }

  // Nothing in this card may reject. The global unhandledrejection handler at the top of
  // this file paints #gate and unhides it, so one stray throw in here would splash an
  // error banner across a screen that is otherwise working. req() never rejects, but the
  // parsing and comparison code above can, so every lane's failure becomes a row state.
  function guard(lane, fn) {
    return Promise.resolve().then(fn).then(null, function (e) {
      status[lane] = { ok: null, name: STATUS_LABEL[lane] + ': could not check',
                       detail: String((e && e.message) || e).slice(0, 200) };
    }).then(function () {
      // Stamped on both paths on purpose: a lane that failed was still sampled, and the
      // report should say when, so "could not check" is not mistaken for "never ran".
      if (lane === 'bundle') { state.bundleCheckedAt = new Date().toISOString(); }
      return renderStatus();
    });
  }

  // Rows land at different times and each render resizes, so they appear as they resolve.
  // The release row needs the app version, so those two are a chain rather than parallel.
  function loadStatus() {
    return Promise.all([
      guard('bundle', checkBundle),
      guard('app', checkAppVersion).then(function () { return guard('release', checkLatestRelease); })
    ]).then(null, function () { /* guard() already rendered the failure */ });
  }

  // ---------------------------------------------------------------- pre-flight

  function renderSteps(ulId, steps) {
    el(ulId).innerHTML = steps.map(function (s) {
      var cls = s.ok === true ? 'ok' : (s.ok === false ? 'bad' : 'skip');
      var mark = s.ok === true ? '✓' : (s.ok === false ? '✗' : '•');
      return '<li><span class="' + cls + '">' + mark + ' ' + esc(s.name) + '</span>' +
             (s.detail ? '<div class="detail">' + esc(s.detail) + '</div>' : '') + '</li>';
    }).join('');
    resize();
  }

  function preflight() {
    var s = state.settings;
    var steps = [];
    var ok = true;

    // 0. what is actually being deployed. Shown first and always, so "field
    //    actions are off" is a visible choice rather than a silent omission.
    var sub = substitute(state.bundleText, s);
    if (sub.mode === 'on') {
      steps.push({ ok: true, name: 'Field actions: ON',
                   detail: 'Deploying the full bundle, including the TSANet Action dropdown flow.' });
    } else if (sub.mode === 'off') {
      steps.push({ ok: null, name: 'Field actions: OFF (optional feature)',
                   detail: 'TSANet Action / Action Text are not configured, so the field-driven flow ' +
                           'is left out: ' + sub.dropped.join(', ') + '. Everything else deploys ' +
                           'normally, and the sidebar app is unaffected — it never uses those fields. ' +
                           'To enable later, create the two fields, enter their IDs in app settings, ' +
                           'and deploy again.' });
    }

    // 1. every substitution input present
    if (sub.missing.length) {
      ok = false;
      steps.push({ ok: false, name: 'App settings complete',
                   detail: 'Required, but not set: ' + sub.missing.join(', ') +
                           '. Set them in Admin Center > Apps > TSANet Connect, then Re-check.' });
    } else if (sub.invalid.length) {
      ok = false;
      steps.push({ ok: false, name: 'App settings well-formed',
                   detail: sub.invalid.join('; ') +
                           '. A value that breaks out of its JSON context would change the ' +
                           'uploaded bundle, so it is rejected here rather than uploaded.' });
    } else if (sub.leftovers.length) {
      ok = false;
      steps.push({ ok: false, name: 'Bundle fully substituted',
                   detail: 'Placeholders still present after substitution: ' + sub.leftovers.join(', ') +
                           '. The embedded bundle and this app version disagree; do not deploy.' });
    } else if (sub.parseError) {
      ok = false;
      steps.push({ ok: false, name: 'Substituted bundle is valid JSON',
                   detail: sub.parseError + '. Nothing will be uploaded.' });
    } else {
      steps.push({ ok: true, name: 'App settings complete, well-formed, and bundle parses' });
    }
    renderSteps('preflight-steps', steps);

    // 2. every configured field id actually exists on this instance. ZIS accepts
    //    a bundle referencing nonexistent field ids and only fails at runtime.
    return req({ url: '/api/v2/ticket_fields.json?page[size]=100', type: 'GET' })
      .then(function (r) {
        if (!r.ok) {
          ok = false;
          steps.push({ ok: false, name: 'Verify custom field IDs', detail: 'HTTP ' + r.status + ' ' + r.body });
          return;
        }
        var live = {};
        ((r.data && r.data.ticket_fields) || []).forEach(function (f) { live[String(f.id)] = f.title; });
        var bad = [];
        Object.keys(FIELD_PLACEHOLDERS).forEach(function (ph) {
          var key = FIELD_PLACEHOLDERS[ph];
          // A USER id, not a ticket-field id (#178) — it can never appear in
          // ticket_fields, so checking it here blocked every shared-author
          // deploy (#226). Check 4b below validates it against /api/v2/users;
          // a non-numeric value is still caught above by check 1 (substitute()
          // rejects any non-blank FIELD_PLACEHOLDERS value that is not digits).
          if (key === 'shared_author_user_id') { return; }
          var id = (s[key] || '').toString().trim();
          if (id && !live[id]) { bad.push(key + '=' + id); }
        });
        if (bad.length) {
          ok = false;
          steps.push({ ok: false, name: 'Verify custom field IDs',
                       detail: 'No such field on this instance: ' + bad.join(', ') });
        } else {
          steps.push({ ok: true, name: 'Verify custom field IDs' });
        }
      })
      .then(function () {
        // 3. the TSANet OAuth connection the bundle references. Advisory only:
        //    the bundle deploys without it, but every TSANet action then fails auth.
        var conn = connName(s);
        return req({ url: '/api/services/zis/connections/' + encodeURIComponent(integrationName(s)) + '?name=' + encodeURIComponent(conn), type: 'GET' })
          .then(function (r) {
            if (r.ok) { steps.push({ ok: true, name: 'TSANet connection "' + conn + '" exists' }); }
            else {
              // ZIS connections are integration-scoped — the URL above embeds the
              // integration name. So on a renamed instance this is not "missing",
              // it is "exists, under the old name, and does not carry over". Say
              // which one it is, because the fix differs: create vs re-create.
              var renamed = integrationName(s) !== DEFAULT_INTEGRATION;
              steps.push({ ok: null, name: 'TSANet connection "' + conn + '" not confirmed',
                           detail: 'HTTP ' + r.status + '. The bundle will deploy, but TSANet actions fail auth ' +
                                   'if this connection is missing. Not blocking.' +
                                   (renamed ? ' This instance uses a non-default integration name (' +
                                    integrationName(s) + '), and connections do not move between ' +
                                    'integrations: a connection created under "' + DEFAULT_INTEGRATION +
                                    '" has to be created again under this one.' : '') });
            }
          });
      })
      .then(function () {
        // 3b. the fixed-name "zendesk" connection, resolved by all seven of the
        //     bundle's Zendesk-side actions (create/search/get/update ticket and
        //     the three finish steps). Same advisory contract as 3 — the bundle deploys
        //     without it — but the runtime consequence is harsher: ZIS refuses to
        //     start the inbound and field-action flows ("Cannot start flow because
        //     the connection named 'zendesk' does not exist"), and the engine names only ONE
        //     missing connection per run, so this and 3 can be a single incident
        //     that reads as two different errors (Zendesk_App#220). The name is a
        //     literal on purpose: only the TSANet connection is per-instance (see
        //     substitute()), and routing this through connName(s) would check the
        //     wrong connection.
        return req({ url: '/api/services/zis/connections/' + encodeURIComponent(integrationName(s)) + '?name=zendesk', type: 'GET' })
          .then(function (r) {
            if (r.ok) { steps.push({ ok: true, name: 'Zendesk connection "zendesk" exists' }); }
            else {
              var renamed = integrationName(s) !== DEFAULT_INTEGRATION;
              steps.push({ ok: null, name: 'Zendesk connection "zendesk" not confirmed',
                           detail: 'HTTP ' + r.status + '. The bundle will deploy, but ZIS refuses to start ' +
                                   'the inbound and field-action flows while this connection is missing. ' +
                                   'Create it per QUICK_START Step 4a. Not blocking.' +
                                   (renamed ? ' This instance uses a non-default integration name (' +
                                    integrationName(s) + '), and connections do not move between ' +
                                    'integrations: a connection created under "' + DEFAULT_INTEGRATION +
                                    '" has to be created again under this one.' : '') });
            }
          });
      })
      .then(function () {
        // 4. downgrade guard. Deploying with field actions off does not uninstall
        //    an already-installed jobspec_field_action — the upload orphans it, and
        //    an orphaned spec still intercepts events while its flow is gone. Say
        //    so before the deploy, not after, because this is usually a mistake:
        //    someone cleared the settings without meaning to turn the feature off.
        if (sub.mode !== 'off') { return; }
        return req({ url: registryPath(s) + '/job_specs', type: 'GET' }).then(function (r) {
          if (!r.ok) { return; }   // advisory only; never block on a failed read
          var live = (r.data && r.data.job_specs) || [];
          var on = live.some(function (j) { return j.installed && j.name === 'jobspec_field_action'; });
          if (on) {
            steps.push({ ok: null, name: 'Field actions are installed but about to be dropped',
                         detail: 'jobspec_field_action is currently installed, and this deploy leaves it ' +
                                 'out. It will be orphaned: still registered, still intercepting ' +
                                 'ticket.CustomFieldChanged, with its flow gone. If you meant to keep ' +
                                 'field actions, set the two field IDs and Re-check. If you meant to ' +
                                 'remove them, uninstall it with DELETE /api/services/zis/registry/' +
                                 'job_specs/install?job_spec_name=' + jobSpecPrefix(s) + 'jobspec_field_action' });
          }
        });
      })
      .then(function () {
        // 4b. shared author check (#178). Advisory: a wrong id cannot break
        //     anything at runtime — Zendesk attributes a nonexistent author_id
        //     to the authenticated user with a 200/201 (probed on d3v
        //     2026-08-07) — but that failure is SILENT, so the one place to
        //     catch a typo'd or suspended user is here, where the admin can
        //     still see the name they actually selected.
        var authorId = (s.shared_author_user_id || '').toString().trim();
        if (!authorId || !/^\d+$/.test(authorId)) { return; }
        return req({ url: '/api/v2/users/' + authorId + '.json', type: 'GET' }).then(function (r) {
          if (!r.ok) {
            steps.push({ ok: null, name: 'Shared author user not found',
                         detail: 'shared_author_user_id ' + authorId + ' did not resolve (HTTP ' +
                                 r.status + '). Zendesk will silently attribute connector comments ' +
                                 'to the connection user instead. Check the id in Admin Center > ' +
                                 'People before deploying.' });
            return;
          }
          var u = (r.data && r.data.user) || {};
          if (u.suspended) {
            steps.push({ ok: null, name: 'Shared author user is suspended',
                         detail: '"' + u.name + '" (' + authorId + ') is suspended. Attribution may ' +
                                 'not behave as intended; unsuspend the user or pick another.' });
          } else {
            steps.push({ ok: true, name: 'Shared author: ' + u.name,
                         detail: 'Connector comments will be attributed to "' + u.name + '" (' +
                                 (u.role || 'unknown role') + ', id ' + authorId + ').' });
          }
        });
      })
      .then(function () {
        // 5. rename guard. Renaming does not retire the old integration: its job
        //    specs stay installed, its flows stay present, and it keeps receiving
        //    the same events — so both integrations process every inbound ping and
        //    the member gets duplicate tickets. verify() reads only the NEW
        //    registry, so it structurally cannot see this. Advisory, and only
        //    meaningful when the name is non-default; a rename from one custom
        //    name to another is undetectable from here (noted in zis/README.md).
        if (integrationName(s) === DEFAULT_INTEGRATION) { return; }
        return req({ url: '/api/services/zis/registry/' + DEFAULT_INTEGRATION + '/job_specs', type: 'GET' })
          .then(function (r) {
            if (!r.ok) { return; }   // advisory only; never block on a failed read
            var installed = ((r.data && r.data.job_specs) || []).filter(function (j) { return j.installed; });
            if (!installed.length) { return; }
            steps.push({ ok: null, name: 'The previous integration "' + DEFAULT_INTEGRATION + '" is still live',
                         detail: installed.length + ' job spec(s) are still installed under it (' +
                                 installed.map(function (j) { return j.name; }).join(', ') + '). They keep ' +
                                 'intercepting the same events, so both integrations will act on every ' +
                                 'inbound collaboration and tickets will be created twice. Uninstall each ' +
                                 'with DELETE /api/services/zis/registry/job_specs/install?job_spec_name=' +
                                 'zis:' + DEFAULT_INTEGRATION + ':job_spec:<name>. Not blocking.' });
          });
      })
      .then(function () {
        state.preflightOk = ok;
        renderSteps('preflight-steps', steps);
        el('deploy-btn').disabled = !ok;
        resize();
      });
  }

  // ------------------------------------------------------------------ deploy

  function deploy() {
    var s = state.settings;
    state.steps = [];
    el('deploy-btn').disabled = true;
    el('recheck-btn').disabled = true;
    // A deploy writes to the registry, so whatever the card established about the
    // running bundle stops being true the moment this starts. Clear it BEFORE the first
    // request rather than refreshing after the last one: otherwise there is a window in
    // which Copy report emits a pre-deploy verdict, and the dangerous case is not the
    // stale "differs" — it is a stale "matches what this app would deploy" carried into
    // the report for a deploy that then failed halfway.
    status.bundle = { ok: null, name: 'Bundle: checking…',
                      detail: 'A deploy is in progress, so the previous result no longer applies.' };
    renderStatus();
    show('result', true);
    el('result-banner').className = 'banner warn';
    el('result-banner').textContent = 'Deploying… do not close this tab.';
    renderSteps('result-steps', state.steps);

    var sub = substitute(state.bundleText, s);
    if (sub.missing.length || sub.invalid.length || sub.leftovers.length || sub.parseError) {
      record('Substitute bundle', false,
             'missing=' + sub.missing.join(',') +
             ' invalid=' + sub.invalid.join('; ') +
             ' leftovers=' + sub.leftovers.join(',') +
             (sub.parseError ? ' parseError=' + sub.parseError : ''));
      return finish();
    }
    record('Substitute bundle', true, 'All per-instance values applied.');

    // The job specs present in the text we are about to upload — which is NOT the
    // same set as the embedded bundle's whenever field actions are off, because
    // stripFieldActions has removed jobspec_field_action with the eight resources
    // it owns. Deriving from state.bundle asked ZIS to install a spec the upload
    // did not contain; ZIS answered 400 "one or more requested job specs is
    // invalid", verify() read the same wrong list and called it missing, and a
    // deploy that had in fact succeeded reported "Integration NOT operational".
    // Field actions off is the DEFAULT for a first-time installer (#132), so this
    // fired on essentially every fresh install; our own instance has them on,
    // which is why an end-to-end run never showed it.
    //
    // Parsed from sub.text rather than recomputed, so there is exactly one
    // derivation and it cannot drift from the artifact. Safe here: the gate above
    // has already returned on sub.parseError. Kept as a local rather than on
    // state: reportText() should keep describing the shipped bundle, preflight()
    // re-runs substitute() freely, and a local cannot go stale across a failed
    // re-deploy.
    var deployedNames = bundleJobSpecNames(JSON.parse(sub.text));

    // 1. integration must exist before the bundle can be uploaded. Idempotent:
    //    an existing integration returns 200, a duplicate returns 409.
    return req({
      url: registryPath(s), type: 'POST', contentType: 'application/json',
      data: JSON.stringify({ description: 'TSANet Connect' })
    }).then(function (r) {
      var ok = r.ok || r.status === 409;
      // The third case, and the one that produced #174. A 400 here does not mean
      // the request was malformed: ZIS returns it when the name belongs to a
      // namespace this account cannot write to, which no amount of permission
      // fixes. Say what to do at the point it happens, because the next failure
      // downstream is a 401 "integration mismatch" that reads like a credential
      // problem and sent a member debugging Entra for two days.
      var detail;
      if (ok) {
        detail = (r.status === 409 ? 'Already existed.' : 'Present.');
      } else if (r.status === 400 && /not available for upsert/i.test(String(r.body || ''))) {
        detail = 'HTTP 400 — the name "' + integrationName(s) + '" is not available to this Zendesk ' +
                 'account. It is not a permissions problem and retrying will not help. Pick a unique ' +
                 'name, register it once with POST /api/services/zis/registry/<name>, put the same ' +
                 'name in the app setting tsanet_integration_name, then Re-check.';
      } else {
        detail = 'HTTP ' + r.status + ' ' + r.body;
      }
      record('Ensure ZIS integration "' + integrationName(s) + '"', ok, detail);
      if (!ok) { throw new Error('integration'); }

      // 2. upload the substituted bundle
      return req({ url: registryPath(s) + '/bundles', type: 'POST', contentType: 'application/json', data: sub.text });
    }).then(function (r) {
      record('Upload bundle', r.ok, r.ok ? 'Accepted.' : ('HTTP ' + r.status + ' ' + r.body));
      if (!r.ok) { throw new Error('upload'); }

      // 3. install EVERY job spec. Uploading orphaned them, so a spec skipped
      //    here stays dead. Attempt all, never abort on the first failure.
      var names = deployedNames;
      return names.reduce(function (chain, name) {
        return chain.then(function () {
          return req({
            url: '/api/services/zis/registry/job_specs/install?job_spec_name=' +
                 encodeURIComponent(jobSpecPrefix(s) + name),
            type: 'POST', contentType: 'application/json'
          }).then(function (r2) {
            record('Install job spec ' + name, r2.ok, r2.ok ? '' : ('HTTP ' + r2.status + ' ' + r2.body));
          });
        });
      }, Promise.resolve());
    }).then(function () {
      return verify(deployedNames);
    }).catch(function () {
      // deployedNames is a closure variable, so it is in scope even when the chain
      // failed before the install loop.
      return verify(deployedNames);   // still read back: a partial state must be reported accurately
      // finish() on BOTH outcomes. The catch above is the last handler in the chain, so
      // a rejection from the read-back it performs would otherwise skip finish entirely:
      // buttons stay disabled, the banner still reads "Deploying…", and since this
      // branch now clears the bundle row up front, the card would go on asserting "a
      // deploy is in progress" forever. A row that lies is worse than one that is stale,
      // and finish() is where the post-deploy re-check fires.
    }).then(finish, finish);
  }

  // Truth comes from the registry, not from the POST responses above.
  function verify(expected) {
    var s = state.settings;
    return req({ url: registryPath(s) + '/job_specs', type: 'GET' }).then(function (r) {
      if (!r.ok) {
        record('Verify installed job specs', false,
               'HTTP ' + r.status + ' ' + r.body + ' — could not confirm state. Treat as NOT deployed.');
        return;
      }
      var live = (r.data && r.data.job_specs) || [];
      var installed = {};
      live.forEach(function (j) { if (j.installed) { installed[j.name] = true; } });

      var missing = expected.filter(function (n) { return !installed[n]; });
      record('Verify installed job specs', missing.length === 0,
             missing.length ? ('Not installed: ' + missing.join(', '))
                            : (expected.length + ' of ' + expected.length + ' installed.'));

      // stale generations still intercept events (zis/README.md)
      var orphans = Object.keys(installed).filter(function (n) { return expected.indexOf(n) === -1; });
      if (orphans.length) {
        state.steps.push({ ok: null, name: 'Stale job specs installed from an older bundle or a dropped feature',
                           detail: orphans.join(', ') + '. These still intercept events. ' +
                                   'Uninstall them with DELETE /api/services/zis/registry/job_specs/install' +
                                   '?job_spec_name=' + jobSpecPrefix(s) + '<name>' });
      }
    });
  }

  function finish() {
    var failed = state.steps.filter(function (s) { return s.ok === false; });
    var b = el('result-banner');
    if (!failed.length) {
      b.className = 'banner good';
      b.textContent = 'Deployed. Bundle uploaded and all job specs confirmed installed.';
      show('retry-btn', false);
    } else {
      b.className = 'banner bad';
      b.textContent = 'Integration NOT operational — ' + failed.length +
                      ' step(s) failed. Job specs may be uninstalled; the integration will not ' +
                      'process events until this succeeds. Retry, then send the report to TSANet.';
      show('retry-btn', true);
    }
    renderSteps('result-steps', state.steps);
    el('recheck-btn').disabled = false;
    el('deploy-btn').disabled = false;
    // Re-read the registry now the writes are done, so the card and the copied report
    // describe what is actually deployed rather than what was there at boot. Bundle lane
    // only: a deploy cannot change this app's installed version or the latest published
    // release, so re-running those two would spend the round-trips and re-expose their
    // failure modes for nothing. Not awaited, and guarded, exactly like the boot call.
    guard('bundle', checkBundle);
    resize();
  }

  function reportText() {
    var s = state.settings || {};
    var lines = [
      'TSANet Connect — ZIS deploy report',
      'when:        ' + new Date().toISOString(),
      'integration: ' + integrationName(s),
      'env:         ' + (s.tsanet_env || '?'),
      'connection:  ' + connName(s),
      // zis_template_version is Zendesk's ZIS template schema constant and is the same
      // for every bundle on the platform. It used to be printed here as though it were a
      // bundle version, which it is not, so it is now labelled as what it is (#171).
      'bundle:      ' + (state.bundle && state.bundle.name) +
                        ' (ZIS template ' + (state.bundle && state.bundle.zis_template_version) + ')',
      'app version: ' + (state.appVersion || '?') +
                        ' (latest release ' + (state.latestTag || '?') + ')',
      // Sampled-at, because this line is the one support reads to decide whether a
      // member needs to deploy, and it is a snapshot of a remote resource rather than a
      // property of the report. The re-checks after deploy and Apply keep it current;
      // the stamp covers the cases they cannot, namely a re-check that failed and a
      // report copied while one is still in flight.
      'deployed:    ' + (status.bundle && status.bundle.name) +
                        (state.bundleCheckedAt ? '  (sampled ' + state.bundleCheckedAt + ')'
                                               : '  (not sampled yet)'),
      ''
    ];
    state.steps.forEach(function (st) {
      lines.push((st.ok === true ? '[ok]   ' : st.ok === false ? '[FAIL] ' : '[warn] ') + st.name);
      if (st.detail) { lines.push('        ' + st.detail); }
    });
    return lines.join('\n');
  }

  // -------------------------------------------------------------------- boot

  function boot() {
    return client.get('currentUser').then(function (d) {
      var u = d && d.currentUser;
      if (!u || u.role !== 'admin') {
        el('gate').textContent =
          'Administrator access required. Deploying the ZIS bundle changes account-level ' +
          'integration configuration, and Zendesk rejects these calls for non-admins.';
        show('gate', true);
        resize();
        return null;
      }
      // no-store: ZAF asset URLs are stable across app updates, so a default-cache
      // fetch right after a ZIP update can return the PREVIOUS version's bundle —
      // which both fakes an in-sync verdict and gets deployed (#217).
      return fetch('./tsanet_connect_bundle.json', { cache: 'no-store' }).then(function (r) { return r.text(); });
    }).then(function (text) {
      if (text == null) { return; }
      state.bundleText = text;
      state.bundle = JSON.parse(text);
      return client.metadata().then(function (md) {
        state.settings = (md && md.settings) || {};
        // Needed to write settings back from Apply. If Zendesk ever stops
        // supplying it, detection still works and only Apply is unavailable.
        state.installationId = (md && md.installationId) || null;
        // Used only to look this app's installed version up. Absent means the App
        // version row degrades; nothing else depends on it.
        state.appId = (md && md.appId) || null;
        if (!state.installationId) {
          el('detect-note').textContent = ' (detect only — no installation id, save manually)';
        }
        show('main', true);
        // Paint the placeholder rows, then fill them in. Deliberately NOT awaited: the
        // card is reference material and must never delay pre-flight or the Deploy
        // button, and every lane inside is guarded so this cannot reject.
        renderStatus();
        loadStatus();
        return preflight();
      });
    }).catch(function (e) {
      el('gate').textContent = 'Could not start: ' + (e && e.message ? e.message : e);
      show('gate', true);
      resize();
    });
  }

  el('deploy-btn').addEventListener('click', deploy);
  el('retry-btn').addEventListener('click', deploy);
  el('recheck-btn').addEventListener('click', function () { preflight(); });
  el('detect-btn').addEventListener('click', function () { detectFields(); });
  el('apply-btn').addEventListener('click', function () { applyDetected(); });
  el('copy-btn').addEventListener('click', function () {
    var t = reportText();
    var done = function () { el('copy-note').textContent = ' copied'; };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(t).then(done, function () { window.prompt('Copy the report:', t); });
    } else { window.prompt('Copy the report:', t); }
  });

  boot();
}());
