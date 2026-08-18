#!/usr/bin/env node
'use strict';

// Config-matrix probes for deploy.js's substitution + drift comparison (#219, #225).
//
// Run:  node scripts/config-matrix-probe.js        (exit 0 = all pass, 1 = a failure)
//
// WHY THIS FILE EXISTS
//
// The deploy screen builds a different bundle per configuration: field actions
// on/off strips resources, auto-accept on/off substitutes a flow flag, and the
// Current state card compares the running bundle against what substitute() would
// produce right now. Every one of those paths is a place where one configuration
// can be correct and another silently broken, and two such defects shipped:
//
//   #225: FIELD_ACTION_RESOURCES claimed exclusive ownership of
//   action_zd_get_ticket after #208's ShowTicket state had begun referencing it
//   from flow_handle_ping — so a field-actions-OFF deploy stripped an action the
//   inbound update path calls, and every such install shipped broken. No gate
//   caught it: substitute()'s missing/invalid/leftovers accounting validates
//   placeholder substitution, not referential integrity of the stripped bundle.
//
//   #219 review: substitute() gained stripActionFieldClears() for field-actions-off
//   bundles, and compareBundle's self-check compared that output against the
//   UN-normalized pristine side — every field-actions-off install would have seen
//   the "cannot verify its own bundle" banner. Green on the default config,
//   broken on the other half of the matrix.
//
//   #226: preflight()'s "Verify custom field IDs" iterated every FIELD_PLACEHOLDERS
//   key, including shared_author_user_id — a USER id that can never appear among
//   ticket-field ids — so any install with the shared-author feature configured
//   was hard-blocked from deploying. The correct check (4b, /api/v2/users) passed
//   right below it. Green on the default config, broken the moment an optional
//   feature was actually used: the same class as the two above, but in the
//   pre-flight rather than the bundle.
//
// So this probe drives the REAL substitute() and compareBundle() through the full
// field-actions x auto-accept matrix and asserts, per configuration: the output
// parses, nothing is left over, the strip sets are exact, every flow state's
// ActionName resolves to a resource that survived the strip, the auto-accept flag
// and nextSteps landed, and compareBundle reads our own output as insync. Two
// drift rows then assert the toggled-but-not-redeployed cases actually produce
// the 'settings' verdict QUICK_START promises.
//
// Node builtins only (fs, path) — no setup-node, no npm install, same contract as
// compare-probe.js.

const fs = require('fs');
const path = require('path');
const ROOT = path.resolve(__dirname, '..');

let src = fs.readFileSync(path.join(ROOT, 'zaf-build/assets/deploy.js'), 'utf8');
const tail = '\n  boot();\n}());';
if (!src.includes(tail)) { console.error('FAIL  deploy.js tail anchor not found (boot() call moved?)'); process.exit(1); }
src = src.replace(tail, '\n  globalThis.__probe = { substitute, fieldActionMode, compareBundle, state, preflight };\n}());');

// Stubs: enough DOM/ZAF for the module to evaluate; boot() is not called.
const elStub = () => ({ addEventListener() {}, classList: { add() {}, remove() {} }, style: {}, textContent: '', innerHTML: '' });
global.window = { addEventListener() {} };
global.document = { getElementById: elStub, addEventListener() {}, createElement: elStub, querySelector: elStub };
// No navigator stub: Node 21+ ships a global `navigator` with only a getter (strict
// mode makes assignment throw), and deploy.js only touches it inside a click handler.
global.ZAFClient = { init: () => ({ on() {}, get: () => new Promise(() => {}), invoke() {}, request: () => new Promise(() => {}), metadata: () => new Promise(() => {}) }) };
eval(src);
const { substitute, compareBundle, state } = globalThis.__probe;

const bundle = fs.readFileSync(path.join(ROOT, 'zaf-build/assets/tsanet_connect_bundle.json'), 'utf8');
const base = {
  tsanet_username: 'api@member.com', tsanet_env: 'BETA',
  field_id_token: '111', field_id_status: '222', field_id_partner: '333',
  field_id_respond_by: '444', field_id_tokens_multi: '555',
  shared_author_user_id: '', tsanet_connection_name: '', tsanet_integration_name: '',
  tsanet_engineer_email: ''
};
const FA = { field_id_action: '666', field_id_action_text: '777' };
const CUSTOM_TEXT = 'Case accepted automatically; an engineer will follow up shortly.';

// The invariant #225's bug violated: every ActionName a flow references must
// resolve to a resource that survived the strip.
function danglingActionRefs(resources) {
  const missing = [];
  for (const [fname, r] of Object.entries(resources)) {
    if (r.type !== 'ZIS::Flow') continue;
    for (const [sname, st] of Object.entries(r.properties.definition.States)) {
      const m = (st.ActionName || '').match(/^zis:[^:]+:action:(.+)$/);
      if (m && !resources[m[1]]) missing.push(`${fname}.${sname} -> ${m[1]}`);
    }
  }
  return missing;
}

let failures = 0;
function report(ok, label, detail) {
  console.log((ok ? 'PASS' : 'FAIL') + '  ' + label + (ok || !detail ? '' : '\n      ' + detail));
  if (!ok) failures++;
}

// The malformed-customFields guard (#223 QA finding) lives inside the jq expr as
// text; assert it stays, since no node-side test can execute jq.
{
  const b = JSON.parse(bundle);
  const expr = b.resources.flow_handle_ping.properties.definition.States.BuildSubmitter.Parameters.expr;
  report(expr.includes('[]? | objects'), 'BuildSubmitter jq keeps the malformed-shape guard ([]? | objects)');
}

for (const fa of [false, true]) for (const aa of [false, true]) {
  const s = Object.assign({}, base, fa ? FA : {}, {
    tsanet_auto_accept: aa,
    tsanet_auto_accept_next_steps: aa ? CUSTOM_TEXT : ''
  });
  const label = `fieldActions=${fa ? 'on' : 'off'} autoAccept=${aa ? 'on' : 'off'}`;
  const sub = substitute(bundle, s);
  const problems = [];
  if (sub.parseError) problems.push('parseError: ' + sub.parseError);
  if (sub.missing.length) problems.push('missing: ' + sub.missing);
  if (sub.invalid.length) problems.push('invalid: ' + sub.invalid);
  if (sub.leftovers.length) problems.push('leftovers: ' + sub.leftovers);
  const res = JSON.parse(sub.text).resources;
  const dangling = danglingActionRefs(res);
  if (dangling.length) problems.push('dangling action refs: ' + dangling.join(', '));
  const exclusives = ['flow_field_action', 'jobspec_field_action', 'action_ts_reject', 'action_ts_info', 'action_zd_finish_note_receipt'];
  for (const n of exclusives) {
    if (fa && !res[n]) problems.push('missing field-action resource: ' + n);
    if (!fa && res[n]) problems.push('should be stripped: ' + n);
  }
  for (const n of ['action_ts_accept', 'action_zd_get_ticket', 'action_zd_finish_status', 'action_zd_finish_fail']) {
    if (!res[n]) problems.push('shared action stripped: ' + n);
  }
  const expr = res.flow_handle_ping.properties.definition.States.BuildSubmitter.Parameters.expr;
  if (!expr.includes(`auto_accept: "${aa ? 'on' : 'off'}"`)) problems.push('auto_accept mode not substituted');
  const accept = JSON.stringify(res.action_ts_accept);
  const expectText = aa ? CUSTOM_TEXT : 'Accepted via Zendesk.';
  if (!accept.includes(JSON.stringify(expectText).slice(1, -1))) problems.push('nextSteps wrong');
  if (aa && accept.includes('Accepted via Zendesk.')) problems.push('default nextSteps survived a custom setting');
  if (!accept.includes('api@member.com')) problems.push('email fallback to username missing');
  // Drift-card self-consistency: our own fresh output must read as insync — the
  // pristine-side-normalization class from the #219 review.
  state.bundleText = bundle; state.bundle = JSON.parse(bundle); state.settings = s;
  const cmp = compareBundle(res);
  if (cmp.verdict !== 'insync') problems.push(`compareBundle self: ${cmp.verdict} (${String(cmp.detail).slice(0, 120)})`);
  report(!problems.length, label, problems.join('\n      '));
}

// Toggled-but-not-redeployed must produce the 'settings' verdict QUICK_START promises.
{
  const sOff = Object.assign({}, base, FA, { tsanet_auto_accept: false, tsanet_auto_accept_next_steps: '' });
  const sOn = Object.assign({}, base, FA, { tsanet_auto_accept: true, tsanet_auto_accept_next_steps: '' });
  const running = JSON.parse(substitute(bundle, sOff).text).resources;
  state.bundleText = bundle; state.bundle = JSON.parse(bundle); state.settings = sOn;
  const cmp = compareBundle(running);
  report(cmp.verdict === 'settings', 'drift: auto-accept toggled, not redeployed -> settings', 'got ' + cmp.verdict);

  const sText = Object.assign({}, base, FA, { tsanet_auto_accept: true, tsanet_auto_accept_next_steps: 'New text.' });
  const running2 = JSON.parse(substitute(bundle, sOn).text).resources;
  state.settings = sText;
  const cmp2 = compareBundle(running2);
  report(cmp2.verdict === 'settings', 'drift: nextSteps edited, not redeployed -> settings', 'got ' + cmp2.verdict);
}

// ------------------------------------------------------------- preflight (#226)
//
// Drive the REAL preflight() against a stubbed healthy instance. The class this
// guards: a pre-flight check false-positive on a valid configuration (#226 —
// check 2 hard-blocked every shared-author install). Each scenario gets a FRESH
// eval of deploy.js (fresh memoized DOM, fresh state) so nothing leaks between
// scenarios, and every network call must hit an explicit route — an unrouted URL
// throws, which rejects the scenario rather than hanging or passing vacuously.

function freshModule() {
  const els = {};
  let router = (opts) => { throw new Error('unrouted: ' + opts.url); };
  global.document = {
    getElementById: (id) => els[id] || (els[id] = elStub()),
    addEventListener() {}, createElement: elStub, querySelector: elStub
  };
  global.ZAFClient = { init: () => ({
    on() {}, get: () => new Promise(() => {}), invoke() {},
    request: (opts) => Promise.resolve().then(() => router(opts)),
    metadata: () => new Promise(() => {})
  }) };
  (0, eval)(src);
  return { probe: globalThis.__probe, els, setRouter: (fn) => { router = fn; } };
}

// Substring-matched routes; first hit wins. A route value is a thunk so a
// rejection is built per call, matching req()'s reject contract ({status,
// responseText}) exactly — client.request REJECTS on non-2xx.
function routes(table) {
  return (opts) => {
    for (const [substr, thunk] of table) {
      if (opts.url.indexOf(substr) !== -1) { return thunk(); }
    }
    throw new Error('unrouted: ' + opts.url);
  };
}

const SA_ID = '90001122334455';
const LIVE_FIELDS = () => Promise.resolve({
  ticket_fields: [111, 222, 333, 444, 555, 666, 777].map((id) => ({ id, title: 'f' + id, type: 'text' }))
});
const HEALTHY_ROUTES = [
  ['/api/v2/ticket_fields.json', LIVE_FIELDS],
  ['/api/services/zis/connections/', () => Promise.resolve({})],
  ['/api/v2/users/' + SA_ID + '.json',
    () => Promise.resolve({ user: { name: 'Partner via TSANet', role: 'end-user', suspended: false } })]
];

function runPreflight(settings, table) {
  const m = freshModule();
  m.setRouter(routes(table));
  m.probe.state.bundleText = bundle;
  m.probe.state.settings = settings;
  const watchdog = new Promise((_, rej) => setTimeout(() => rej(new Error('preflight timed out (5s) — a request neither resolved nor rejected')), 5000));
  return Promise.race([m.probe.preflight(), watchdog]).then(() => ({
    ok: m.probe.state.preflightOk,
    deployDisabled: els(m, 'deploy-btn').disabled,
    html: els(m, 'preflight-steps').innerHTML
  }));
  function els(mod, id) { return mod.els[id] || {}; }
}

(async () => {
  const saBase = Object.assign({}, base, FA, {
    tsanet_auto_accept: false, tsanet_auto_accept_next_steps: '',
    shared_author_user_id: SA_ID
  });

  // (a) The #226 case: fully configured healthy instance, shared author SET.
  //     Must be deployable, via the RIGHT check: 4b resolved the user.
  try {
    const r = await runPreflight(saBase, HEALTHY_ROUTES);
    const problems = [];
    if (r.ok !== true) problems.push('preflightOk=' + r.ok);
    if (r.deployDisabled !== false) problems.push('deploy-btn.disabled=' + r.deployDisabled);
    if (r.html.indexOf('No such field') !== -1) problems.push('check 2 false-positived: ' + r.html.match(/No such field[^<]*/));
    if (r.html.indexOf('Shared author: Partner via TSANet') === -1) problems.push('check 4b did not resolve the shared author');
    report(!problems.length, 'preflight: shared author configured, healthy instance -> Deploy enabled', problems.join('\n      '));
  } catch (e) { report(false, 'preflight: shared author configured, healthy instance -> Deploy enabled', String(e)); }

  // (b) Check 2 keeps its recall, with precision: a genuinely wrong field id
  //     still blocks, and the failure names ONLY the field id, never the
  //     shared author. This is the assertion the #226 fix's revert flips.
  try {
    const r = await runPreflight(Object.assign({}, saBase, { field_id_token: '999' }), HEALTHY_ROUTES);
    const problems = [];
    if (r.ok !== false) problems.push('preflightOk=' + r.ok + ' (bogus field id was not caught)');
    if (r.deployDisabled !== true) problems.push('deploy-btn.disabled=' + r.deployDisabled);
    if (r.html.indexOf('field_id_token=999') === -1) problems.push('failure detail does not name field_id_token=999');
    if (r.html.indexOf('shared_author_user_id=') !== -1) problems.push('check 2 still flags shared_author_user_id');
    report(!problems.length, 'preflight: bogus field id blocks; shared author not blamed', problems.join('\n      '));
  } catch (e) { report(false, 'preflight: bogus field id blocks; shared author not blamed', String(e)); }

  // (c) 4b's advisory polarity: an unresolvable shared author user warns but
  //     never blocks (a wrong id is SILENT at runtime, not broken — probed on
  //     d3v 2026-08-07; see the 4b comment in deploy.js).
  try {
    const table = [
      ['/api/v2/users/', () => Promise.reject({ status: 404, responseText: '{"error":"RecordNotFound"}' })]
    ].concat(HEALTHY_ROUTES);
    const r = await runPreflight(saBase, table);
    const problems = [];
    if (r.ok !== true) problems.push('preflightOk=' + r.ok + ' (advisory check blocked the deploy)');
    if (r.deployDisabled !== false) problems.push('deploy-btn.disabled=' + r.deployDisabled);
    if (r.html.indexOf('Shared author user not found') === -1) problems.push('missing the advisory row');
    report(!problems.length, 'preflight: shared author 404 warns, does not block', problems.join('\n      '));
  } catch (e) { report(false, 'preflight: shared author 404 warns, does not block', String(e)); }

  // (d) The coverage the #226 fix must NOT lose: a non-numeric shared author id
  //     is still rejected — by check 1 (substitute()'s digits-only validation),
  //     not by check 2's field lookup.
  try {
    const r = await runPreflight(Object.assign({}, saBase, { shared_author_user_id: 'abc' }), HEALTHY_ROUTES);
    const problems = [];
    if (r.ok !== false) problems.push('preflightOk=' + r.ok + ' (non-numeric id was not caught)');
    if (r.html.indexOf('must be digits only') === -1) problems.push('check 1 did not reject the non-numeric id');
    if (r.html.indexOf('shared_author_user_id=abc') !== -1) problems.push('check 2 flagged it (should be check 1)');
    report(!problems.length, 'preflight: non-numeric shared author id still blocks via check 1', problems.join('\n      '));
  } catch (e) { report(false, 'preflight: non-numeric shared author id still blocks via check 1', String(e)); }

  // (e) The default half of the class: shared author UNSET must stay green,
  //     with no shared-author row at all (4b skips blanks).
  try {
    const r = await runPreflight(Object.assign({}, saBase, { shared_author_user_id: '' }), HEALTHY_ROUTES);
    const problems = [];
    if (r.ok !== true) problems.push('preflightOk=' + r.ok);
    if (r.deployDisabled !== false) problems.push('deploy-btn.disabled=' + r.deployDisabled);
    if (r.html.indexOf('Shared author') !== -1) problems.push('a shared-author row rendered for a blank setting');
    report(!problems.length, 'preflight: shared author unset stays green, no 4b row', problems.join('\n      '));
  } catch (e) { report(false, 'preflight: shared author unset stays green, no 4b row', String(e)); }

  process.exit(failures ? 1 : 0);
})();
