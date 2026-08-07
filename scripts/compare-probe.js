#!/usr/bin/env node
'use strict';

// Regression probes for the deploy screen's bundle comparison (#171, #172).
//
// Run:  node scripts/compare-probe.js        (exit 0 = all pass, 1 = a failure)
//
// WHY THIS FILE EXISTS
//
// The comparison answers one question for the admin: is the ZIS bundle running on this
// instance the one this app would deploy? A wrong answer is expensive in both
// directions. A false "differs" invites a needless deploy, and an upload orphans the
// installed job specs before the new ones go in. A false "matches" hides a bundle that
// is not the one the app's code expects.
//
// Two defects reached review, and BOTH were invisible to every gate in the repo:
//
//   #175 made the ZIS integration name per-instance. It taught substitute() to rewrite
//   the 18 resource ARNs and did not teach the comparison, so every renamed instance
//   compared 18 ARNs literally, failed, and was told to redeploy a bundle that was
//   already correct. zaf-bundle-sync could not see it: that gate asks whether each
//   placeholder token appears ANYWHERE in deploy.js, which the top-of-file constants
//   satisfy no matter what the comparison code does.
//
//   #175 also deleted `var REGISTRY` in favour of registryPath(s). This branch had
//   added two new uses of REGISTRY after the fork point, so git had nothing to conflict
//   on and the merge was clean. The result threw ReferenceError inside a guarded lane,
//   which renders "could not check" on an otherwise green screen.
//
// So these probes drive the REAL deploy.js under stubs rather than reimplementing the
// comparison. The thing under test is the shipped file, including its boot wiring and
// its lane guards, because both defects lived in exactly that wiring.

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const DEPLOY_JS = path.join(ROOT, 'zaf-build/assets/deploy.js');
const BUNDLE_JSON = path.join(ROOT, 'zaf-build/assets/tsanet_connect_bundle.json');

const SRC = fs.readFileSync(DEPLOY_JS, 'utf8');
const BUNDLE_TEXT = fs.readFileSync(BUNDLE_JSON, 'utf8');

// deploy.js is an IIFE with no exports, and it must stay that way — it is a browser
// asset with no build step. To reach its internals we append an assignment just before
// the file's own boot() call. This edits the string we evaluate here, never the file.
const BOOT_CALL = '\n  boot();\n}());';
if (SRC.indexOf(BOOT_CALL) === -1) {
  console.error('probe: could not find the boot() call to hook. deploy.js structure changed.');
  process.exit(1);
}
function instrument(src) {
  return src.replace(
    BOOT_CALL,
    '\n  __probe.expose({ SUBST_SITES: SUBST_SITES, substitute: substitute,' +
    ' shapeEq: shapeEq, shapePattern: shapePattern });\n  boot();\n}());'
  );
}

const DEFAULT_SETTINGS = {
  tsanet_env: 'PRODUCTION',
  tsanet_engineer_email: 'ops@example.com',
  tsanet_connection_name: 'tsanet_oauth',
  field_id_token: '111111',
  field_id_action: '222222',
  field_id_status: '333333',
  field_id_partner: '444444',
  field_id_respond_by: '555555',
  field_id_action_text: '666666'
};

// ---------------------------------------------------------------- the stub environment

function makeElement() {
  const e = {
    textContent: '', innerHTML: '', value: '', disabled: false, checked: false,
    scrollHeight: 100,
    style: {},
    classList: { add() {}, remove() {}, contains() { return false; } },
    // Captured so probes can drive the real click handlers (Deploy, Apply) rather than
    // calling internals the file does not export.
    __handlers: [],
    addEventListener(_type, fn) { this.__handlers.push(fn); },
    appendChild() {},
    querySelector() { return makeElement(); }
  };
  return e;
}

// Let every pending lane settle. The screen fires its checks without awaiting them, so
// drain the microtask queue rather than depending on a promise the file does not return.
function drain(turns) {
  let p = Promise.resolve();
  for (let i = 0; i < (turns || 6); i++) {
    p = p.then(() => new Promise((r) => setImmediate(r)));
  }
  return p;
}

// Runs the real deploy.js to completion against a scripted set of responses.
// `routes` maps a URL substring to a handler returning the response body, or throwing
// an object {status, responseText} to simulate an HTTP failure.
function run(opts) {
  const els = Object.create(null);
  const requests = [];
  const settings = opts.settings || DEFAULT_SETTINGS;
  let exposed = null;

  const document = {
    getElementById(id) {
      if (!els[id]) { els[id] = makeElement(); }
      return els[id];
    },
    body: { scrollHeight: 100 },
    addEventListener() {}
  };

  function request(o) {
    const url = String(o && o.url || '');
    requests.push(url);
    for (const key of Object.keys(opts.routes)) {
      if (url.indexOf(key) !== -1) {
        // A real ZAF client always REJECTS; it never throws synchronously. Keeping that
        // faithful matters, because req() normalises a rejection into {ok:false} and a
        // synchronous throw would escape it — a failure mode the app cannot actually
        // produce, which would make any probe built on it prove nothing.
        try { return Promise.resolve(opts.routes[key](url, exposed)); }
        catch (e) { return Promise.reject({ status: 500, responseText: String(e.message || e) }); }
      }
    }
    // Unrouted calls (pre-flight's job-spec reads, mostly) fail like a real 404. The
    // lanes under test are guarded, and pre-flight is not what these probes assert.
    return Promise.reject({ status: 404, responseText: 'probe: unrouted ' + url });
  }

  const client = {
    get() { return Promise.resolve({ currentUser: { role: 'admin' } }); },
    metadata() {
      return Promise.resolve({ settings: settings, installationId: '1', appId: '9' });
    },
    request: request,
    invoke() {},
    on() {}
  };

  const sandbox = {
    document,
    console,
    setTimeout,
    navigator: {},
    // deploy.js installs window.onerror / unhandledrejection handlers at load.
    addEventListener() {},
    prompt() {},
    ZAFClient: { init: () => client },
    fetch: () => Promise.resolve({ text: () => Promise.resolve(BUNDLE_TEXT) }),
    __probe: { expose(o) { exposed = o; } }
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;

  let src = instrument(SRC);
  if (opts.patch) { src = opts.patch(src); }
  vm.runInNewContext(src, sandbox, { filename: 'deploy.js' });

  const snapshot = () => ({
    rows: els['status-rows'] ? els['status-rows'].innerHTML : '',
    gate: els['gate'] ? els['gate'].textContent : '',
    banner: els['result-banner'] ? els['result-banner'].textContent : '',
    requests,
    exposed,
    // Fire the real handler the file registered, then let the chain settle.
    click: (id) => {
      const handlers = els[id] ? els[id].__handlers : [];
      handlers.forEach((fn) => fn());
      return drain(12).then(snapshot);
    }
  });
  return drain().then(snapshot);
}

// The Bundle row is the first <li> of the status list.
function bundleRow(rows) {
  const m = /<li><span class="[^"]*">.(\s*Bundle:[^<]*)<\/span>(?:<div class="detail">([^<]*)<\/div>)?/.exec(rows);
  if (!m) { return { name: '(no Bundle row rendered)', detail: '' }; }
  return { name: m[1].trim(), detail: (m[2] || '').trim() };
}

// Build the "running" bundle the way a real deploy does: run the app's own substitution
// over the shipped bundle. In-sync BY CONSTRUCTION, so any verdict other than "matches"
// is the comparison failing to recognise the app's own output.
function deployedFrom(exposed, settings) {
  const sub = exposed.substitute(BUNDLE_TEXT, settings);
  if (sub.missing.length || sub.invalid.length || sub.leftovers.length || sub.parseError) {
    throw new Error('probe fixture is not deployable: ' + JSON.stringify({
      missing: sub.missing, invalid: sub.invalid,
      leftovers: sub.leftovers, parseError: sub.parseError
    }));
  }
  return JSON.parse(sub.text);
}

// Two-pass: pass 1 gets `substitute` out of the module so the fixture can be built with
// it, pass 2 serves that fixture. The registry routes are only reached in pass 2.
async function withDeployedBundle(settings, mutate) {
  const first = await run({ settings, routes: {} });
  if (!first.exposed) { throw new Error('probe: internals were not exposed'); }
  const deployed = deployedFrom(first.exposed, settings);
  if (mutate) { mutate(deployed); }
  return deployed;
}

function registryRoutes(deployed) {
  return {
    '/bundles/': () => deployed,
    '/bundles': () => ({ bundles: [{ uuid: 'u1', name: 'tsanet', zis_template_version: '2020-01-01' }] }),
    '/api/v2/apps/': () => ({ version: '1.0.63' }),
    'api.github.com': () => ({ tag_name: 'v1.0.63', html_url: 'https://example.invalid' })
  };
}

// ------------------------------------------------------------------------- the probes

// A deploy chain that rejects past its own catch produces an unhandled rejection. In a
// browser that reaches the handler deploy.js installs at load; under node it would kill
// the harness before the probe could assert anything. Collect them instead, so the
// failure is observable rather than fatal.
const unhandled = [];
process.on('unhandledRejection', (e) => { unhandled.push(String((e && e.message) || e)); });

const results = [];
function check(name, pass, got, want) {
  results.push({ name, pass, got, want });
  console.log((pass ? 'PASS  ' : 'FAIL  ') + name);
  if (!pass) {
    console.log('        want: ' + want);
    console.log('        got:  ' + got);
  }
}

async function main() {
  // 1. Baseline. Default integration name, bundle in sync by construction.
  {
    const deployed = await withDeployedBundle(DEFAULT_SETTINGS);
    const r = await run({ settings: DEFAULT_SETTINGS, routes: registryRoutes(deployed) });
    const row = bundleRow(r.rows);
    check('default name, in-sync bundle reads as matching',
          /matches what this app would deploy/.test(row.name),
          row.name + ' | ' + row.detail, 'Bundle: matches what this app would deploy');
  }

  // 2. BLOCKER 2 regression (#175's ARN substitution site).
  //    A renamed instance whose bundle is in sync by construction must read as matching.
  //    Before the SUBST_SITES table this printed "Definitions differ" for all six flows
  //    and job specs, and recommended a redeploy that would have been a pure outage.
  const renamed = Object.assign({}, DEFAULT_SETTINGS, { tsanet_integration_name: 'acme_tsanet' });
  {
    const deployed = await withDeployedBundle(renamed);
    const r = await run({ settings: renamed, routes: registryRoutes(deployed) });
    const row = bundleRow(r.rows);
    check('renamed integration, in-sync bundle reads as matching (#175 ARN site)',
          /matches what this app would deploy/.test(row.name),
          row.name + ' | ' + row.detail, 'Bundle: matches what this app would deploy');
  }

  // 3. BLOCKER 1 regression (REGISTRY removed by #175).
  //    The bundle lane must reach the RENAMED registry path. A ReferenceError here is
  //    swallowed by guard() and renders "could not check" while the screen stays green,
  //    so assert the request URLs as well as the absence of that row.
  //
  //    UNIVERSAL, not existential. The first version of this asserted that SOME request
  //    hit /registry/acme_tsanet/bundles. checkBundle has two call sites and the second
  //    one's URL contains the first one's substring, so either alone satisfied that, and
  //    mutation-testing confirmed it: hardcoding the default name at exactly one site
  //    passed 12/12 twice. Blocker 1 was "a call site reaching a registry path that no
  //    longer resolves", so the assertion has to be that NO call site does, which also
  //    covers sites added later. That is the real failure mode: both of blocker 1's call
  //    sites were themselves added after the fork point.
  {
    const deployed = await withDeployedBundle(renamed);
    const r = await run({ settings: renamed, routes: registryRoutes(deployed) });
    const row = bundleRow(r.rows);
    //    One call legitimately names the DEFAULT integration and must not be swept up:
    //    pre-flight's rename guard (deploy.js:1183) deliberately reads the OLD
    //    integration on a renamed instance, because renaming does not retire it and its
    //    job specs keep intercepting the same events, producing duplicate tickets. A
    //    blanket "no call names the default" assertion fails on correct code, and
    //    satisfying it would delete that safeguard.
    const RENAME_GUARD = '/api/services/zis/registry/' + 'tsanet_connect' + '/job_specs';
    const registryCalls = r.requests.filter((u) => u.indexOf('/registry/') !== -1);
    const scoped = (u) => u.indexOf('/registry/acme_tsanet/') !== -1;

    // Blocker 1's own class: every registry call the bundle lane makes. Both of
    // checkBundle's call sites have to fire, so a single satisfied URL cannot stand in
    // for the other.
    const bundleCalls = registryCalls.filter((u) => u.indexOf('/bundles') !== -1);
    check('every bundle-lane registry call is scoped to the instance name',
          bundleCalls.length >= 2 && bundleCalls.every(scoped),
          JSON.stringify(bundleCalls),
          'both the list and the per-bundle read, each under /registry/acme_tsanet/');

    // And nothing else quietly hardcodes the default. Catches a call site added later,
    // which is the real failure mode: both of blocker 1's sites were themselves added
    // after the fork point.
    const unexpected = registryCalls.filter((u) => !scoped(u) && u !== RENAME_GUARD);
    check('no registry call other than the rename guard names the default integration',
          unexpected.length === 0, JSON.stringify(unexpected),
          'only deploy.js:1183, the deliberate rename guard');
    check('bundle lane does not fall into "could not check"',
          !/could not check/.test(row.name),
          row.name + ' | ' + row.detail, 'any verdict other than "could not check"');
  }

  // 4. A genuine content difference is reported as a difference, with no claim about
  //    which side is newer. shapeEq establishes only that the two differ.
  {
    const deployed = await withDeployedBundle(DEFAULT_SETTINGS, (b) => {
      const first = Object.keys(b.resources)[0];
      b.resources[first].__probe_added_key = 'this side carries something the app does not';
    });
    const r = await run({ settings: DEFAULT_SETTINGS, routes: registryRoutes(deployed) });
    const row = bundleRow(r.rows);
    check('a differing bundle is reported as differing',
          /differs from what this app would deploy/.test(row.name),
          row.name, 'Bundle: differs from what this app would deploy');
    check('the difference verdict claims no direction and recommends nothing',
          !/older|newer|recommend/i.test(row.name),
          row.name, 'no "older" / "newer" / "recommend" in the row');
  }

  // 5. The self-check. Simulate the NEXT #175 by deleting the ARN site from the
  //    comparator's vocabulary while leaving substitute() untouched — precisely the
  //    state this branch was in before the fix. The screen must refuse to classify
  //    rather than confidently mis-advise.
  {
    const dropArn = (src) => src.replace(/\{ token: ARN_PREFIX,\s*shape: '[^']*' \}/, '{ token: \'__probe_never_matches__\', shape: \'x\' }');
    if (dropArn(SRC) === SRC) {
      check('self-check probe could patch SUBST_SITES', false, 'no substitution made',
            'the ARN entry to be removable by the probe');
    } else {
      const deployed = await withDeployedBundle(renamed);
      const r = await run({ settings: renamed, routes: registryRoutes(deployed), patch: dropArn });
      const row = bundleRow(r.rows);
      check('a comparator that fell behind substitute() self-reports instead of mis-advising',
            /cannot be checked by this app version/.test(row.name),
            row.name + ' | ' + row.detail,
            'Bundle: cannot be checked by this app version');
    }
  }

  // 6. shapePattern applies sites in sequence over one string, and split/join does not
  //    rescan inserted text — but a LATER site's token could still occur inside an
  //    EARLIER site's inserted shape. True today by luck of the values, not by design.
  {
    const first = await run({ settings: DEFAULT_SETTINGS, routes: {} });
    const sites = first.exposed.SUBST_SITES;
    const collisions = [];
    sites.forEach((a) => sites.forEach((b) => {
      if (a !== b && a.shape.indexOf(b.token) !== -1) {
        collisions.push(b.token + ' occurs inside the shape for ' + a.token);
      }
    }));
    check('no substitution shape contains another site\'s token',
          collisions.length === 0, JSON.stringify(collisions), 'no collisions');
    check('every substitution site is reachable from one table',
          sites.length === 10, sites.length + ' sites',
          '10 (6 field ids + shared author + host + email + ARN)');
  }

  // 7. The bundle row is cleared at deploy start, which means a deploy that never
  //    reaches finish() leaves a row asserting "checking…" forever — a status that lies
  //    rather than merely being stale. finish() is where the re-check fires, so it must
  //    run on the rejection path too, not only on fulfilment.
  {
    // req() normalises every HTTP outcome, so no ROUTE can make the chain reject; the
    // rejection has to come from inside verify()'s own callback. Nothing in today's
    // verify() can throw on any input a route can supply, so this patches verify() to
    // reject — the same technique probe 5 uses. What is under test is the chain's
    // terminator, not verify's contents: today's verify is not the only verify this
    // code will ever have.
    const rejectInVerify = (src) =>
      src.replace('function verify(expected) {',
                  'function verify(expected) { return Promise.reject(new Error("probe: forced")); //');
    const deployed = await withDeployedBundle(DEFAULT_SETTINGS);
    const r = await run({
      settings: DEFAULT_SETTINGS, routes: registryRoutes(deployed), patch: rejectInVerify
    });
    const after = await r.click('deploy-btn');
    const row = bundleRow(after.rows);
    check('a deploy that rejects past its own catch still re-checks the bundle row',
          !/A deploy is in progress/.test(row.detail),
          row.name + ' | ' + row.detail,
          'not left asserting "a deploy is in progress"');
  }

  // 8. Apply saves settings and THEN re-derives the screen. If that re-derivation
  //    rejects, the settings are already saved, so a verdict computed against the old
  //    ones must not survive on screen. Same shape as probe 7, and the injection is
  //    needed for the same reason: req() normalises every HTTP outcome, so no route can
  //    make preflight() reject. Detection has to succeed first, or applyDetected()
  //    returns at its guard and the probe asserts nothing — which is exactly what the
  //    first version of this probe did, and the negative control is what caught it.
  {
    const rejectInPreflight = (src) =>
      src.replace('function preflight() {',
                  'function preflight() { return Promise.reject(new Error("probe: forced")); //');
    const deployed = await withDeployedBundle(DEFAULT_SETTINGS);
    // Titles match EXPECTED_FIELDS; ids differ from DEFAULT_SETTINGS so Detect resolves
    // a non-empty change set and state.detected is populated.
    const ticketFields = {
      ticket_fields: [
        { id: 900001, title: 'TSANet Token', type: 'text' },
        { id: 900002, title: 'TSANet Status', type: 'tagger' },
        { id: 900003, title: 'TSANet Partner', type: 'text' },
        { id: 900004, title: 'TSANet Respond By', type: 'date' }
      ]
    };
    const routes = Object.assign(registryRoutes(deployed), {
      '/api/v2/ticket_fields': () => ticketFields,
      '/api/v2/apps/installations/': () => ({})
    });
    const r = await run({ settings: DEFAULT_SETTINGS, routes, patch: rejectInPreflight });
    const detected = await r.click('detect-btn');
    const after = await detected.click('apply-btn');
    const row = bundleRow(after.rows);
    check('Apply re-checks the bundle row even when the follow-up rejects',
          !/App settings just changed/.test(row.detail),
          row.name + ' | ' + row.detail,
          'not left asserting "app settings just changed"');
  }

  // 9. The self-check walks subRes by key and compares against the pristine side. A
  //    substitution pass that rewrote a resource KEY rather than a value would produce a
  //    name the pristine side lacks; skipping those silently is the blind spot. The
  //    field-id and ARN passes run over the bundle TEXT, so this is not structurally
  //    prevented — assert it is detected.
  {
    const renameKey = (src) => src.replace(
      'return { text: out, missing: missing,',
      'out = out.replace(\'"flow_handle_ping"\', \'"flow_handle_ping_probe"\');\n' +
      '    return { text: out, missing: missing,'
    );
    if (renameKey(SRC) === SRC) {
      check('key-rewrite probe could patch substitute()', false, 'no substitution made',
            'substitute()\'s return to be patchable');
    } else {
      const deployed = await withDeployedBundle(DEFAULT_SETTINGS);
      const r = await run({ settings: DEFAULT_SETTINGS, routes: registryRoutes(deployed), patch: renameKey });
      const row = bundleRow(r.rows);
      check('a substitution that rewrites a resource KEY is caught, not skipped',
            /cannot be checked by this app version/.test(row.name) && /renamed the resource/.test(row.detail),
            row.name + ' | ' + row.detail,
            'the selfcheck verdict naming the renamed resource');
    }
  }

  const failed = results.filter((r) => !r.pass);
  console.log('\n' + (results.length - failed.length) + '/' + results.length + ' probes passed');
  process.exit(failed.length ? 1 : 0);
}

main().catch((e) => { console.error('probe error:', e && e.stack || e); process.exit(1); });
