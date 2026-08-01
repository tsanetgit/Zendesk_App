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
    ' fitPanelToContent: (typeof fitPanelToContent === "function") ? fitPanelToContent : null,' +
    ' PANEL_MIN_H: (typeof PANEL_MIN_H === "number") ? PANEL_MIN_H : null,' +
    ' PANEL_MAX_H: (typeof PANEL_MAX_H === "number") ? PANEL_MAX_H : null,' +
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

const TOKEN = 'cd277fac1b44aa00';

function makeElement(id) {
  return {
    id,
    textContent: '', innerHTML: '', value: '', disabled: false,
    style: {}, className: '',
    classList: { add() {}, remove() {}, contains() { return false; } },
    addEventListener() {}, removeEventListener() {},
    appendChild() {}, insertBefore() {}, removeChild() {},
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
  let exposed = null;

  const body = makeElement('body');
  // The one input the height fit reads. Scenarios set it to stand in for content.
  body.scrollHeight = 300;

  const document = {
    getElementById(id) {
      if (!els[id]) els[id] = makeElement(id);
      return els[id];
    },
    createElement: (t) => makeElement('<' + t + '>'),
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
    calls, resizes, body, exposed,
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

  // 10. flexible_height is not a ZAF manifest property, so declaring it did nothing
  //     for every release that carried it. Assert against the documented key set
  //     rather than just its absence, so the next invented key is caught too.
  //
  //     Every app in the repo, DISCOVERED rather than named. This read a hardcoded
  //     zaf-build/manifest.json and passed while zaf-debug/manifest.json sat in the same
  //     tree declaring flexible_height: a check reporting "nothing found" with the thing
  //     it looks for present, which is the shape it exists to catch. Top-level
  //     directories only, because one app per top-level dir is this repo's convention
  //     and, with no ignore logic available here, a recursive walk would collect stray
  //     manifest.json files from fixtures or dependencies. A nested app would be missed.
  //
  //     Every PRODUCT under location, not just support. `location` is keyed by product
  //     (support, chat, sell), so pinning "support" leaves the identical blind spot one
  //     level up, where location.chat.invented_key would sail through.
  {
    const ALLOWED = ['url', 'autoHide', 'autoLoad', 'flexible', 'signed', 'size'];
    const appDirs = fs.readdirSync(ROOT, { withFileTypes: true })
      .filter((e) => e.isDirectory() && fs.existsSync(path.join(ROOT, e.name, 'manifest.json')))
      .map((e) => e.name)
      .sort();

    // Without this, a scan that finds nothing is indistinguishable from a scan that
    // finds nothing wrong, and the check below passes while measuring zero manifests.
    // Named rather than counted: a third app is covered by the scan automatically, and
    // removing one takes a deliberate edit here rather than silently shrinking coverage.
    //
    // It also converts the one silent miss the scan has into a loud one. Dirent does not
    // follow symlinks, so a symlinked app directory reports isDirectory() false and drops
    // out of appDirs; for the two apps named here that now fails this check instead of
    // passing quietly. A FUTURE app added as a symlink would still be missed.
    check('the manifest scan discovers every app in this repo',
          ['zaf-build', 'zaf-debug'].every((d) => appDirs.indexOf(d) !== -1),
          JSON.stringify(appDirs), 'at least zaf-build and zaf-debug');

    const bad = [];
    appDirs.forEach((dir) => {
      const rel = dir + '/manifest.json';
      let mf;
      // Has to land as a failed check: a throw would kill every probe after this one,
      // and skipping would be a vacuous pass.
      try { mf = JSON.parse(fs.readFileSync(path.join(ROOT, rel), 'utf8')); }
      catch (e) { bad.push(rel + ': unparseable'); return; }
      const byProduct = (mf.location && typeof mf.location === 'object') ? mf.location : {};
      Object.keys(byProduct).forEach((product) => {
        const locs = byProduct[product];
        if (!locs || typeof locs !== 'object') { bad.push(rel + ' ' + product + ': not an object'); return; }
        Object.keys(locs).forEach((loc) => {
          const v = locs[loc];
          // A bare-string location ("background": "assets/background.html") carries no
          // properties, so there is nothing here to check.
          if (!v || typeof v !== 'object') { return; }
          Object.keys(v).forEach((k) => {
            if (ALLOWED.indexOf(k) === -1) { bad.push(rel + ' ' + product + '.' + loc + '.' + k); }
          });
        });
      });
    });
    check('no manifest location declares a property ZAF does not define',
          bad.length === 0, JSON.stringify(bad),
          'only ' + ALLOWED.join('/') + ', across ' + appDirs.length + ' manifest(s)');
  }

  const failed = results.filter((r) => !r.pass);
  console.log('\n' + (results.length - failed.length) + '/' + results.length + ' probes passed');
  process.exit(failed.length ? 1 : 0);
}

main().catch((e) => { console.error('probe error:', (e && e.stack) || e); process.exit(1); });
