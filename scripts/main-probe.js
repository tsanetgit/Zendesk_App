#!/usr/bin/env node
'use strict';

// Regression probes for the ticket sidebar (zaf-build/assets/main.js).
//
// Run:  node scripts/main-probe.js        (exit 0 = all pass, 1 = a failure)
//
// WHY THIS FILE EXISTS
//
// tsanetgit/Zendesk_App#169: outbound submit creates the collaboration case FIRST and
// then does bookkeeping on the member's own ticket. Every step shared one outer
// `.catch`, so a rejection from the bookkeeping rendered "Submit failed" beside a
// dialog still populated with the same payload. The natural response to that is to
// press Submit again, which opens a SECOND collaboration request to the partner. The
// case had already been created and the partner already had it.
//
// That is a promise-chain shape, not a value, and reading a chain is exactly the kind
// of check that looks right and is not. So these probes drive the real `handleSubmit`
// under a stubbed ZAF client and DOM, and assert on what the agent would actually see:
// the banner text and whether the dialog is still there to resubmit from.
//
// main.js is an IIFE with no exports and must stay that way (browser asset, no build
// step), so the probe appends an expose call to the string it evaluates. The file on
// disk is never modified.

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const MAIN_JS = path.join(ROOT, 'zaf-build/assets/main.js');
const SRC = fs.readFileSync(MAIN_JS, 'utf8');

const IIFE_END = '\n})();';
if (!SRC.endsWith(IIFE_END) && SRC.indexOf(IIFE_END) === -1) {
  console.error('probe: could not find the IIFE terminator in main.js; structure changed.');
  process.exit(1);
}
function instrument(src) {
  const at = src.lastIndexOf(IIFE_END);
  return src.slice(0, at) +
    // typeof guards so this harness also loads against a version that predates the
    // fixes. Without them the expose throws a ReferenceError at load and the whole
    // suite reports nothing at all, which is indistinguishable from passing -- the
    // exact failure this file exists to avoid. Absent symbols surface as failed
    // probes instead.
    '\n__probe.expose({ handleSubmit: handleSubmit,' +
    ' loadCollaborations: (typeof loadCollaborations === "function") ? loadCollaborations : null,' +
    ' doPartnerSearch: (typeof doPartnerSearch === "function") ? doPartnerSearch : null,' +
    ' selectPartner: (typeof selectPartner === "function") ? selectPartner : null,' +
    ' fitPanelToContent: (typeof fitPanelToContent === "function") ? fitPanelToContent : null,' +
    ' PANEL_MIN_H: (typeof PANEL_MIN_H === "number") ? PANEL_MIN_H : null,' +
    ' PANEL_MAX_H: (typeof PANEL_MAX_H === "number") ? PANEL_MAX_H : null,' +
    ' loadNotes: (typeof loadNotes === "function") ? loadNotes : null,' +
    ' setState: function (o) {' +
    ' if (o.settings !== undefined) settings = o.settings;' +
    ' if (o.currentForm !== undefined) currentForm = o.currentForm;' +
    ' if (o.selectedPartner !== undefined) selectedPartner = o.selectedPartner; } });' +
    src.slice(at);
}

const SETTINGS = {
  tsanet_env: 'BETA',
  tsanet_username: 'ops@example.com',
  field_id_token: '111111',
  field_id_tokens_multi: '222222',
  field_id_status: '333333',
  field_id_partner: '444444',
  field_id_respond_by: '555555'
};

// Fabricated fixture, never a real case token. Kept at 16 lowercase hex so the
// probe still exercises the real token shape, but written so that no reader has
// to ask: the previous literal was realistic enough that a security review had
// to probe BETA to find out, and that probe could not answer, because a made-up
// control returned the same "not found".
const TOKEN = 'deadbeefdeadbeef';

function makeElement(id) {
  // appendChild RECORDS its children. It used to be a no-op, and that is not a detail:
  // doPartnerSearch builds result rows with createElement + appendChild rather than by
  // assigning innerHTML, so a no-op made those rows invisible to the content model and
  // the probe for tsanetgit/Zendesk_App#188's second half passed against the very bug it
  // was written for. innerHTML is an accessor for the same reason: assigning it clears
  // children in a browser, and the model has to agree or the two paths disagree about
  // what is on screen.
  const children = [];
  let html = '';
  return {
    id,
    textContent: '', value: '', disabled: false,
    style: {}, className: '',
    children,
    get innerHTML() { return html; },
    set innerHTML(v) { html = String(v == null ? '' : v); children.length = 0; },
    classList: { add() {}, remove() {}, contains() { return false; } },
    addEventListener() {}, removeEventListener() {},
    appendChild(c) { children.push(c); return c; },
    insertBefore(c) { children.push(c); return c; },
    removeChild() {},
    setAttribute() {}, querySelector() { return null; }, querySelectorAll() { return []; },
    firstChild: null, nodeType: 1
  };
}

function drain(turns) {
  let p = Promise.resolve();
  for (let i = 0; i < (turns || 10); i++) p = p.then(() => new Promise((r) => setImmediate(r)));
  return p;
}

// `fail` names which step should reject: 'post' (pre-creation), 'writeFields',
// 'addTag', 'loadCollaborations', or null for the happy path.
function run(fail, throwOnResize, patch) {
  const els = Object.create(null);
  const calls = [];
  const resizes = [];
  const needed = [];   // content height at each resize, parallel to `resizes`
  let exposed = null;

  const body = makeElement('body');
  // Content height is DERIVED from what is on screen, not handed in as a number.
  //
  // It used to be `body.scrollHeight = 300`, a plain input. That is why 22 probes
  // passed on a panel that was 18px too short in the one state a new ticket ever
  // reaches (tsanetgit/Zendesk_App#188): with the content height supplied by the
  // scenario, nothing could observe a resize asking for less than the content needs.
  //
  // These are measured, not invented. zaf-build/assets/index.html rendered at 320px,
  // the ZAF sidebar width, each element shown alone and the delta in
  // document.body.scrollHeight recorded. The model is additive and was checked against
  // the composite states: 16+39+40+84+47+101 = 327 matches the measured dialog state,
  // and +4*27 = 435 matches it with four search results.
  const BODY_PAD_H = 16;                 // body { padding: 8px }, top + bottom
  const CONTENT_H = {
    'loading': 64, 'error-banner': 28, 'info-banner': 101, 'compact-bar': 46,
    'btn-new-collab': 39, 'btn-sync-inbound': 40, 'tsanet-notice': 84,
    'empty-state': 47, 'new-collab-dialog': 101, 'collab-form': 0
  };
  const PARTNER_ROW_H = 27;
  const PARTNER_RESULTS_MAX_H = 150;     // #partner-results { max-height: 150px }
  // An EMPTY collab-form measures 0, which is why the table above lists it as 0. Its
  // rendered rows are what carry height: measured 58 for a select, 56 for a text input,
  // 89 for the textarea and 30 for the actions row, so a minimal three-field form plus
  // actions is 233px. Averaged per child rather than typed, because the model only has
  // to make "was the form on screen when this was measured" observable, and with a flat
  // 0 it was not: reverting the selectPartner ordering changed nothing the probe saw.
  const FORM_CHILD_H = 58;
  let forcedScrollHeight = null;
  Object.defineProperty(body, 'scrollHeight', {
    get() {
      if (forcedScrollHeight !== null) { return forcedScrollHeight; }
      let h = BODY_PAD_H;
      Object.keys(CONTENT_H).forEach((id) => {
        const e = els[id];
        // Absent or display:none means not laid out. Elements start hidden here, which
        // matches index.html's CSS defaults for every id in this table.
        if (e && e.style && e.style.display && e.style.display !== 'none') h += CONTENT_H[id];
      });
      // Rows arrive two ways: appended elements (the results list) and an innerHTML
      // string (the Searching / No results / failure placeholders). Count both.
      const cf = els['collab-form'];
      if (cf && cf.style && cf.style.display && cf.style.display !== 'none') {
        h += cf.children.length * FORM_CHILD_H;
      }
      const pr = els['partner-results'];
      if (pr) {
        const appended = pr.children.filter((c) => c && c.className === 'partner-result').length;
        const inHtml = (String(pr.innerHTML).match(/partner-result/g) || []).length;
        const rows = appended + inHtml;
        if (rows) { h += Math.min(PARTNER_RESULTS_MAX_H, rows * PARTNER_ROW_H); }
        else if (pr.innerHTML) { h += PARTNER_ROW_H; }
      }
      return h;
    },
    // heightFor() drives the clamp arithmetic directly, so an explicit assignment still
    // overrides the model.
    set(v) { forcedScrollHeight = v; }
  });

  const document = {
    getElementById(id) {
      if (!els[id]) els[id] = makeElement(id);
      return els[id];
    },
    createElement: (t) => makeElement('<' + t + '>'),
    // selectPartner clears the previous selection through this. Elements already carry
    // it; the document did not, and its absence threw before the fit under test ran.
    querySelectorAll: () => [],
    querySelector: () => null,
    body,
    addEventListener() {}
  };

  // Zendesk-side calls. writeFields and addTicketTag are both PUT to the same ticket
  // URL, so they are told apart by what they write, which is also the only thing that
  // distinguishes them at runtime.
  function request(o) {
    const url = String((o && o.url) || '');
    const type = (o && o.type) || 'GET';
    const data = String((o && o.data) || '');
    calls.push(type + ' ' + url + (data.indexOf('custom_fields') !== -1 ? ' [fields]'
                                : data.indexOf('tags') !== -1 ? ' [tags]' : ''));

    // getJwt reads `accessToken`, not `token`. Getting this wrong makes every probe
    // fail with "TSANet login returned no token" rather than the thing under test.
    if (url.indexOf('/login') !== -1) return Promise.resolve({ accessToken: 'jwt-probe' });
    if (type === 'PUT' && data.indexOf('custom_fields') !== -1) {
      return fail === 'writeFields'
        ? Promise.reject({ status: 422, responseText: 'probe: field write refused' })
        : Promise.resolve({ ticket: {} });
    }
    if (type === 'PUT' && data.indexOf('tags') !== -1) {
      return fail === 'addTag'
        ? Promise.reject({ status: 409, responseText: 'probe: safe_update conflict' })
        : Promise.resolve({ ticket: {} });
    }
    if (url.indexOf('/api/v2/tickets/') !== -1 && type === 'GET') {
      return Promise.resolve({ ticket: { id: 42, tags: ['billing'], custom_fields: [] } });
    }
    if (url.indexOf('/api/v2/search') !== -1) {
      return fail === 'loadCollaborations'
        ? Promise.reject({ status: 500, responseText: 'probe: search down' })
        : Promise.resolve({ results: [] });
    }
    return Promise.resolve({});
  }

  const client = {
    get(keys) {
      const k = [].concat(keys);
      const out = {};
      if (k.indexOf('ticket.id') !== -1) out['ticket.id'] = 42;
      if (k.indexOf('currentUser') !== -1) out['currentUser'] = { name: 'Probe Agent', role: 'admin' };
      if (k.indexOf('ticket.customField:custom_field_111111') !== -1) out[k[0]] = TOKEN;
      return Promise.resolve(out);
    },
    request,
    metadata: () => Promise.resolve({ settings: SETTINGS }),
    invoke(name, opts) {
      if (name !== 'resize') return;
      resizes.push((opts && opts.height) || '');
      // What the content actually needed at the instant the app asked. Paired with the
      // ask, this is what makes "asked for less than is on screen" observable at all.
      needed.push(body.scrollHeight);
      // deploy.js:222 already wraps this call in try/catch, so the repo treats a
      // throwing resize as a real condition. Scenarios opt in to reproduce it.
      if (throwOnResize) throw new Error('ZAF: resize rejected');
    },
    on() {}, has() { return false; }
  };

  // TSANet-side calls go through global fetch, not the ZAF client.
  const fetchStub = (url, opts) => {
    calls.push(((opts && opts.method) || 'GET') + ' ' + url);
    if (String(url).indexOf('/collaboration-requests') !== -1 && opts && opts.method === 'POST') {
      if (fail === 'post') {
        return Promise.resolve({
          ok: false, status: 400,
          json: () => Promise.resolve({ detail: 'probe: partner refused the request' })
        });
      }
      return Promise.resolve({ ok: true, status: 201, json: () => Promise.resolve({ token: TOKEN }) });
    }
    if (String(url).indexOf('/notes') !== -1) {
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(
        [1, 2].map((n) => ({ summary: 'Probe note ' + n, description: 'body ' + n,
                             createdAt: '2026-07-31T17:30:00Z', companyName: 'Probe Partner' }))) });
    }
    // Four rows, which is the case measured against the real page: the dialog needs
    // 327px empty and 435px with these, against a panel last fitted at 347.
    if (String(url).indexOf('/partners/') !== -1) {
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(
        [1, 2, 3, 4].map((n) => ({ companyName: 'Probe Partner ' + n, companyId: 900 + n }))) });
    }
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
  };

  const sandbox = {
    document, console, setTimeout, clearTimeout, fetch: fetchStub,
    navigator: {}, addEventListener() {}, alert() {},
    ZAFClient: { init: () => client },
    __probe: { expose(o) { exposed = o; } }
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;

  var src = instrument(SRC);
  if (patch) src = patch(src);
  vm.runInNewContext(src, sandbox, { filename: 'main.js' });
  if (!exposed) throw new Error('probe: main.js internals were not exposed');

  // Put the form in the state a real submit starts from, without running boot.
  exposed.setState({
    settings: SETTINGS,
    currentForm: { documentId: 'doc-1', customFields: [] },
    selectedPartner: { companyId: 1079, departmentId: null }
  });
  els['form-summary'] = makeElement('form-summary');
  els['form-summary'].value = 'Probe summary';
  els['form-description'] = makeElement('form-description');
  els['form-description'].value = 'Probe description';
  els['form-priority'] = makeElement('form-priority');
  els['form-priority'].value = 'MEDIUM';
  els['new-collab-dialog'] = makeElement('new-collab-dialog');
  els['new-collab-dialog'].style.display = 'block';

  exposed.handleSubmit();

  return drain(16).then(() => ({
    banner: els['error-banner'] ? els['error-banner'].textContent : '',
    dialogDisplay: els['new-collab-dialog'].style.display,
    submitBtn: els['btn-submit-collab'] || makeElement('btn-submit-collab'),
    calls, resizes, needed, body, els, exposed,
    collabPosts: calls.filter((c) => c.indexOf('POST') === 0 && c.indexOf('/collaboration-requests') !== -1).length
  }));
}

// Height fit in isolation: set a content height, ask for a fit, read what was requested.
function heightFor(scrollHeight) {
  return run(null).then((r) => {
    if (!r.exposed.fitPanelToContent) {
      return { asked: '(main.js has no fitPanelToContent)', min: null, max: null };
    }
    r.body.scrollHeight = scrollHeight;
    r.resizes.length = 0;
    r.exposed.fitPanelToContent();
    return { asked: r.resizes[r.resizes.length - 1], min: r.exposed.PANEL_MIN_H, max: r.exposed.PANEL_MAX_H };
  });
}

const results = [];
function check(name, pass, got, want) {
  results.push({ name, pass });
  console.log((pass ? 'PASS  ' : 'FAIL  ') + name);
  if (!pass) { console.log('        want: ' + want); console.log('        got:  ' + got); }
}

async function main() {
  // 1. Happy path, so the probes below are measuring a deviation from something real.
  {
    const r = await run(null);
    check('happy path reports success and closes the dialog',
          /submitted!/.test(r.banner) && r.dialogDisplay === 'none' && r.collabPosts === 1,
          r.banner + ' | dialog=' + r.dialogDisplay + ' | posts=' + r.collabPosts,
          'success banner, dialog closed, exactly one POST');
  }

  // 2 and 3. THE #169 CASES. The case exists; only the ticket bookkeeping failed. The
  //    agent must not be told the submit failed, and must not be left holding a
  //    populated dialog whose Submit button opens a second request to the partner.
  for (const step of ['addTag', 'writeFields']) {
    const r = await run(step);
    check(step + ' failure is not reported as a failed submit',
          !/Submit failed/.test(r.banner),
          r.banner, 'a banner that does not say "Submit failed"');
    check(step + ' failure still closes the dialog, so a resend is unavailable',
          r.dialogDisplay === 'none',
          'dialog=' + r.dialogDisplay, 'dialog closed');
    check(step + ' failure names the token and says not to resend',
          r.banner.indexOf(TOKEN) !== -1 && /[Dd]o NOT submit again/.test(r.banner),
          r.banner, 'the token, and an explicit instruction not to resubmit');
  }

  // NOTE, deliberately not a probe. The chain ends with a terminal .catch after
  // loadCollaborations(), so a refresh failure cannot reach the outer catch and be
  // reported as "Submit failed". That guard is defence in depth and CANNOT be probed
  // today, because loadCollaborations swallows its own errors (main.js:311) and always
  // resolves. A probe here passed against the unfixed file too, which is what exposed
  // it as decorative, so it was removed rather than left to look like coverage.
  //
  // Worth knowing while reading the fix: because that internal catch calls showError,
  // a refresh failure DOES replace the green success banner with a red load error.
  // Pre-existing, out of scope for #169, and noted on the PR.

  // 5. The other direction, and the reason the outer catch still exists: when the POST
  //    itself fails, NO case was created, so "Submit failed" is correct AND the dialog
  //    must stay open, because retrying is the right thing to do.
  {
    const r = await run('post');
    check('a pre-creation failure still reports "Submit failed"',
          /Submit failed/.test(r.banner),
          r.banner, 'Submit failed: ...');
    check('a pre-creation failure leaves the dialog open so a retry is possible',
          r.dialogDisplay === 'block',
          'dialog=' + r.dialogDisplay, 'dialog still open');
    check('a pre-creation failure re-enables the Submit button',
          r.submitBtn.disabled === false && r.submitBtn.textContent === 'Submit',
          'disabled=' + r.submitBtn.disabled + ' text=' + r.submitBtn.textContent,
          'enabled, labelled Submit');
    check('a pre-creation failure created no case',
          r.collabPosts === 1,
          'posts=' + r.collabPosts, 'the one attempt, which failed');
  }

  // ── tsanetgit/Zendesk_App#179, the sidebar's fixed height ──────────────────
  //
  // The pixel-level fit is NOT testable here and is not claimed to be: the apps tray
  // geometry is what is being fitted, so "a single-field form does not require
  // scrolling" needs a real instance. What is testable, and what actually regressed,
  // is that the height is DERIVED rather than constant, and bounded.

  // 6. The constant is gone. This is the whole defect: 600px pads a short form and
  //    truncates a long one, and no content can change either outcome.
  {
    const shortPanel = await heightFor(120);
    const tallPanel = await heightFor(500);
    check('panel height varies with content instead of being a constant',
          shortPanel.asked !== tallPanel.asked &&
            parseInt(shortPanel.asked, 10) < parseInt(tallPanel.asked, 10),
          '120px content -> ' + shortPanel.asked + ', 500px content -> ' + tallPanel.asked,
          'two different heights, the taller content taller');
  }

  // 7. Bounded above, so a ticket with many collaborations scrolls inside the pane
  //    rather than pushing the rest of the apps tray out of reach.
  {
    const huge = await heightFor(100000);
    check('panel height is clamped above',
          parseInt(huge.asked, 10) === huge.max,
          huge.asked, huge.max + 'px');
  }

  // 8. Bounded below, so a fit can never collapse the panel to nothing.
  {
    const tiny = await heightFor(0);
    check('panel height is clamped below',
          parseInt(tiny.asked, 10) === tiny.min,
          tiny.asked, tiny.min + 'px');
  }

  // 9. No path still asks for a literal height. This used to carry an exception for
  //    the collapsed bar's '44px'; that line now reads PANEL_MIN_H, so the check is
  //    unconditional and the two 44s can no longer drift apart unnoticed.
  //
  //    It also asserts there is exactly ONE client.invoke('resize') in the file. The
  //    guard against a throwing resize lives in setPanelHeight, and a guard is only
  //    worth what its call sites are: the first version of this fix guarded
  //    fitPanelToContent and left the collapsed-bar call raw, which a probe caught.
  //    One call site makes that unbypassable rather than merely documented.
  {
    const src = fs.readFileSync(MAIN_JS, 'utf8');
    const hardcoded = src.match(/invoke\('resize'[^)]*height:\s*'(\d+)px'/g) || [];
    check('no resize call hardcodes a panel height',
          hardcoded.length === 0, JSON.stringify(hardcoded),
          'every resize height derived from a constant or from content');

    const invocations = src.match(/client\.invoke\('resize'/g) || [];
    check('every resize goes through the single guarded helper',
          invocations.length === 1, invocations.length + " client.invoke('resize') call(s)",
          'exactly 1, inside setPanelHeight');
  }

  // 11. THE #169/#179 COLLISION. fitPanelToContent() is called inside handleSubmit's
  //     post-creation region, which must never reject. Its last statement is
  //     client.invoke('resize'), which deploy.js:222 already guards, so the repo treats
  //     it as a call that can throw. Unguarded here, a throw reaches the outer .catch
  //     and renders "Submit failed" for a case the partner already has: #169 arriving
  //     back through the fix for #179.
  //
  //     Reproduced before the guard was added: happy path gave
  //     "Submit failed: ZAF: resize rejected | dialog=none | posts=1".
  {
    const r = await run(null, true);
    check('a throwing resize does not turn a created case into "Submit failed"',
          !/Submit failed/.test(r.banner),
          r.banner + ' | posts=' + r.collabPosts,
          'the success banner, with the resize failure swallowed as cosmetic');
    check('a throwing resize still reports success and closes the dialog',
          /submitted!/.test(r.banner) && r.dialogDisplay === 'none',
          r.banner + ' | dialog=' + r.dialogDisplay,
          'success banner, dialog closed');
  }

  // 12. A resize that throws must not break the bookkeeping-failure path either, which
  //     is where the token and the do-not-resend instruction live.
  {
    const r = await run('addTag', true);
    check('a throwing resize leaves the partial-success message intact',
          !/Submit failed/.test(r.banner) && r.banner.indexOf(TOKEN) !== -1,
          r.banner, 'the partial-success message, naming the token');
  }

  // 13. The terminal .catch is defence in depth and nothing reaches it today, because
  //     loadCollaborations has its own terminal catch (main.js:345) and always resolves.
  //     An empty handler there would swallow the outcome entirely: no banner, dialog
  //     closed, case created — which reads as an ordinary success and is the one result
  //     worse than a wrong label, since it carries no signal at all. Injected rather
  //     than routed, because no route can make that chain reject; the alternative was
  //     leaving the branch unprobed, which is how the decorative probe got written the
  //     first time.
  {
    const rejectRefresh = (src) => src.replace(
      'function loadCollaborations(quiet) {',
      'function loadCollaborations(quiet) { return Promise.reject(new Error("probe: forced refresh failure")); //'
    );
    const r = await run(null, false, rejectRefresh);
    check('a refresh that rejects still tells the agent something',
          r.banner.length > 0 && !/Submit failed/.test(r.banner) && r.banner.indexOf(TOKEN) !== -1,
          JSON.stringify(r.banner),
          'a banner naming the token, and never "Submit failed"');
  }

  // 10-11. tsanetgit/Zendesk_App#188. Every resize must ask for at least the content that
  //     is on screen when it fires. The harness could not express this while content
  //     height was an input rather than a model (see CONTENT_H in run()), which is why
  //     22 probes passed on a panel 18px too short in the state a new ticket lands in.
  //
  //     Both halves of #188 are instances of the one invariant, so they are asserted as
  //     one shape rather than two special cases.
  const shortfalls = (r) => r.resizes
    .map((h, i) => ({ asked: parseInt(h, 10), needed: r.needed[i] }))
    .filter((x) => x.asked < x.needed);

  //     shortfalls() alone is NOT enough, and the gap is this patch's own failure mode.
  //     It samples only the instants a resize fired, so a path that fits EARLY and then
  //     renders more content passes: the early ask covered the content that existed
  //     then, and nothing asks about the content that exists now. Deleting the terminal
  //     fit from doPartnerSearch, which is exactly #188's second half, left this suite
  //     at 23/23 until this check was added. So every probe below also asserts the LAST
  //     ask still covers the state the run ended in.
  const coversFinalState = (r) => r.resizes.length > 0 &&
    parseInt(r.resizes[r.resizes.length - 1], 10) >= r.body.scrollHeight;

  //     A NEW ticket carries no TSANet tokens, so loadCollaborations takes the collapsed
  //     branch and no other. The bar measures 62px at the 320px sidebar width; the code
  //     used to assert PANEL_MIN_H, which is 44.
  {
    const r = await run(null);
    if (!r.exposed.loadCollaborations) {
      check('the collapsed compact bar asks for at least its own height',
            false, '(main.js does not expose loadCollaborations)', 'a fitted collapsed state');
    } else {
      r.resizes.length = 0; r.needed.length = 0;
      await r.exposed.loadCollaborations();
      await drain(8);
      const short = shortfalls(r);
      check('the collapsed compact bar asks for at least its own height',
            short.length === 0 && coversFinalState(r),
            JSON.stringify({ asked: r.resizes, needed: r.needed, short,
                             finalNeed: r.body.scrollHeight }),
            'a resize >= the content on screen, not an asserted constant');
    }
  }

  //     And one step later: the dialog is fitted while its results list is empty, then
  //     the search renders into it. Nothing re-fitted, so the panel kept the empty
  //     height. Drives the real doPartnerSearch against the four-row fixture.
  {
    const r = await run(null);
    if (!r.exposed.doPartnerSearch) {
      check('a partner search that renders results refits the panel',
            false, '(main.js does not expose doPartnerSearch)', 'a fit after the results render');
    } else {
      // The state enterNewCollaboration() leaves behind: dialog open, results empty.
      const SHOWN = ['btn-new-collab', 'btn-sync-inbound', 'tsanet-notice', 'empty-state',
                     'new-collab-dialog'];
      SHOWN.concat(['partner-results']).forEach((id) => {
        if (!r.els[id]) r.els[id] = makeElement(id);
      });
      SHOWN.forEach((id) => { r.els[id].style.display = 'block'; });
      r.els['partner-results'].innerHTML = '';
      r.resizes.length = 0; r.needed.length = 0;
      await r.exposed.doPartnerSearch('probe');
      await drain(8);
      const short = shortfalls(r);
      check('a partner search that renders results refits the panel',
            short.length === 0 && coversFinalState(r),
            JSON.stringify({ asked: r.resizes, needed: r.needed, short,
                             finalNeed: r.body.scrollHeight }),
            'the LAST ask >= the content once the results are in the DOM');
    }
  }

  //     And the third instance, found while fixing the two the ticket names: selecting a
  //     partner fetched the form, called renderCollabForm (which fits at its end), and
  //     only THEN set the form visible, so that fit measured a panel with no form in it.
  //     The same shape as the pre-#183 enterNewCollaboration.
  {
    const r = await run(null);
    if (!r.exposed.selectPartner) {
      check('selecting a partner fits with the form visible',
            false, '(main.js does not expose selectPartner)', 'a fit that measures the shown form');
    } else {
      ['btn-new-collab', 'btn-sync-inbound', 'tsanet-notice', 'new-collab-dialog',
       'collab-form', 'partner-results'].forEach((id) => {
        if (!r.els[id]) r.els[id] = makeElement(id);
      });
      ['btn-new-collab', 'btn-sync-inbound', 'tsanet-notice', 'new-collab-dialog']
        .forEach((id) => { r.els[id].style.display = 'block'; });
      r.resizes.length = 0; r.needed.length = 0;
      const itemEl = makeElement('a-result');
      await r.exposed.selectPartner({ companyName: 'Probe Partner 1', companyId: 901 }, itemEl);
      await drain(10);
      check('selecting a partner fits with the form visible',
            shortfalls(r).length === 0 && coversFinalState(r),
            JSON.stringify({ asked: r.resizes, needed: r.needed,
                             finalNeed: r.body.scrollHeight }),
            'the last ask >= the content with collab-form displayed');
    }
  }

  //     And the cap itself. PANEL_MAX_H was chosen for the collaboration LIST and then
  //     applied to the form too, where an inner scrollbar is the complaint #179 opened
  //     with. Reading all 110 live partner forms on BETA, 46 need more than 800px and
  //     the tallest needs 1409 (16 custom fields), so a form-shaped cap is what the
  //     tallest known form has to clear, not a number near today's data.
  {
    const r = await run(null);
    if (!r.exposed.PANEL_MAX_H) {
      check('the tallest known partner form is not truncated',
            false, '(main.js has no PANEL_MAX_H)', 'a cap that a form clears');
    } else {
      ['btn-new-collab', 'btn-sync-inbound', 'tsanet-notice', 'new-collab-dialog',
       'collab-form'].forEach((id) => {
        if (!r.els[id]) r.els[id] = makeElement(id);
        r.els[id].style.display = 'block';
      });
      // 3 always-present fields + 16 custom + the actions row, which is Test Evernex,
      // the tallest form in the member base.
      const form = r.els['collab-form'];
      form.innerHTML = '';
      for (let i = 0; i < 20; i++) { form.appendChild(makeElement('row' + i)); }
      r.resizes.length = 0; r.needed.length = 0;
      r.exposed.fitPanelToContent();
      const asked = parseInt(r.resizes[r.resizes.length - 1], 10);
      const need = r.body.scrollHeight;
      check('the tallest known partner form is not truncated',
            r.resizes.length > 0 && asked >= need,
            JSON.stringify({ asked, need, cap: r.exposed.PANEL_MAX_H }),
            'an ask >= the form height, under the cap of ' + r.exposed.PANEL_MAX_H);
    }
  }

  //     Notes land AFTER the panel has been fitted. renderAll builds each card with a
  //     "Loading notes..." placeholder, loadCollaborations fits, and loadNotes then
  //     replaces the placeholder with the real thread: one card measures 386px at the
  //     placeholder and 510px with two notes, so the panel sat 104px short and clipped
  //     the thread mid-note (tsanetgit/Zendesk_App#191). The bug is a MISSING call, so
  //     what this asserts is that a fit fires in the window where the notes render.
  {
    const r = await run(null);
    if (!r.exposed.loadNotes) {
      check('rendering notes refits the panel',
            false, '(main.js does not expose loadNotes)', 'a fit after the notes render');
    } else {
      // ORDERING, not just presence. "a resize happened while loadNotes ran" would
      // still pass if the fit were moved ahead of the innerHTML write, which is #191
      // exactly. So record how many resizes had fired at the moment the notes landed in
      // the DOM, and require at least one more after that.
      const container = makeElement('notes-probe');
      let html = '';
      let resizesWhenNotesLanded = null;
      Object.defineProperty(container, 'innerHTML', {
        get() { return html; },
        set(v) {
          html = String(v == null ? '' : v);
          if (html.indexOf('Probe note') !== -1) resizesWhenNotesLanded = r.resizes.length;
        }
      });
      r.resizes.length = 0;
      await r.exposed.loadNotes('probe-token', container, 'Us');
      await drain(8);
      const after = resizesWhenNotesLanded === null ? null : r.resizes.length - resizesWhenNotesLanded;
      check('rendering notes refits the panel AFTER they are in the DOM',
            resizesWhenNotesLanded !== null && after > 0,
            JSON.stringify({ resizes: r.resizes, atNotesLanded: resizesWhenNotesLanded,
                             firedAfter: after }),
            'at least one resize after the notes were written, not merely during');
    }
  }

  // 15. flexible_height is not a ZAF manifest property, so declaring it did nothing
  //     for every release that carried it. Assert against the documented key set
  //     rather than just its absence, so the next invented key is caught too.
  {
    const mf = JSON.parse(fs.readFileSync(path.join(ROOT, 'zaf-build/manifest.json'), 'utf8'));
    const ALLOWED = ['url', 'autoHide', 'autoLoad', 'flexible', 'signed', 'size'];
    const bad = [];
    const support = (mf.location && mf.location.support) || {};
    Object.keys(support).forEach((loc) => {
      const v = support[loc];
      if (v && typeof v === 'object') {
        Object.keys(v).forEach((k) => { if (ALLOWED.indexOf(k) === -1) bad.push(loc + '.' + k); });
      }
    });
    check('no manifest location declares a property ZAF does not define',
          bad.length === 0, JSON.stringify(bad),
          'only ' + ALLOWED.join('/'));
  }

  const failed = results.filter((r) => !r.pass);
  console.log('\n' + (results.length - failed.length) + '/' + results.length + ' probes passed');
  process.exit(failed.length ? 1 : 0);
}

main().catch((e) => { console.error('probe error:', (e && e.stack) || e); process.exit(1); });
