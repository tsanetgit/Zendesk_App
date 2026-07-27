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
 * - Expected-installed is derived from the embedded bundle. A job spec that is
 *   installed but absent from the bundle is a stale orphan from an older
 *   generation and still intercepts events (see zis/README.md), so it is
 *   surfaced as a warning instead of being ignored or silently removed.
 * - nav_bar is visible to every agent, and this project has not found an
 *   admin-only ZAF location. The role check below is therefore a UX gate, not a
 *   security boundary. The boundary is server-side: Zendesk documents the ZIS
 *   registry endpoints as "Allowed for: Admins"
 *   (https://developer.zendesk.com/api-reference/integration-services/registry/bundles/).
 *   That is vendor documentation, not a runtime probe by this project — an
 *   agent-role session has not been tested against these endpoints (#125).
 */
/* global ZAFClient */
(function () {
  'use strict';

  var client = ZAFClient.init();

  // Fixed constant, not per-instance: it is embedded in 18 ZIS resource ARNs
  // inside the bundle (zis:tsanet_connect:action:*, :flow:*), hardcoded
  // throughout ZIS_Quick_Start.md, and absent from the per-instance
  // substitution table in zis/README.md. Making it per-instance would require
  // rewriting every ARN too.
  var INTEGRATION = 'tsanet_connect';

  var REGISTRY = '/api/services/zis/registry/' + INTEGRATION;
  var JOB_SPEC_PREFIX = 'zis:' + INTEGRATION + ':job_spec:';

  // Bundle placeholder -> app setting holding the real value.
  var FIELD_PLACEHOLDERS = {
    '1234567890': 'field_id_token',
    '1234567891': 'field_id_action',
    '1234567892': 'field_id_status',
    '1234567893': 'field_id_partner',
    '1234567894': 'field_id_respond_by',
    '1234567895': 'field_id_action_text'
  };

  var HOST_PLACEHOLDER = 'connect2.tsanet.org';   // bundle ships Production
  var CONN_PLACEHOLDER = 'tsanet_oauth';
  var EMAIL_PLACEHOLDER = 'YOUR_TSANET_API_EMAIL';

  var state = { settings: null, bundleText: null, bundle: null, steps: [], preflightOk: false };

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
  // Layer 3 does NOT catch the vector above on its own; layer 2 is what kills
  // it. Layer 3 turns the remaining malformed cases from fail-open to
  // fail-closed.
  //
  // The host is not user input (it is derived from tsanet_env and both values
  // are literals in this file), so it needs no validation.

  // Escape for interpolation into a JSON string literal.
  function jsonStr(v) {
    var q = JSON.stringify(String(v));
    return q.slice(1, q.length - 1);
  }

  function substitute(text, s) {
    var out = text;
    var missing = [];
    var invalid = [];

    Object.keys(FIELD_PLACEHOLDERS).forEach(function (ph) {
      var key = FIELD_PLACEHOLDERS[ph];
      var val = (s[key] || '').toString().trim();
      if (!val) { missing.push(key); return; }
      // Field ids land in an unquoted numeric position. Anything non-numeric
      // either corrupts the JSON or injects structure, so refuse it here.
      if (!/^\d+$/.test(val)) {
        invalid.push(key + ' must be digits only (Zendesk field id)');
        return;
      }
      // \b so a placeholder can never match inside a longer numeric id.
      out = out.replace(new RegExp('\\b' + ph + '\\b', 'g'), val);
    });

    var host = (s.tsanet_env === 'PRODUCTION') ? 'connect2.tsanet.org' : 'connect2.tsanet.net';
    out = out.split(HOST_PLACEHOLDER).join(host);

    // Only the TSANet connection is per-instance. The Zendesk-side connection is
    // named "zendesk" and is fixed, so match the key/value pair rather than the
    // bare string to avoid collateral edits.
    var conn = (s.tsanet_connection_name || CONN_PLACEHOLDER).trim();
    if (/["\\\u0000-\u001f]/.test(conn)) {
      invalid.push('tsanet_connection_name contains a quote, backslash or control character');
    } else {
      out = out.replace(
        new RegExp('("connectionName"\\s*:\\s*)"' + CONN_PLACEHOLDER + '"', 'g'),
        '$1"' + jsonStr(conn) + '"'
      );
    }

    var email = (s.tsanet_engineer_email || '').trim();
    if (!email) {
      missing.push('tsanet_engineer_email');
    } else if (/["\\\u0000-\u001f]/.test(email)) {
      invalid.push('tsanet_engineer_email contains a quote, backslash or control character');
    } else {
      out = out.split(EMAIL_PLACEHOLDER).join(jsonStr(email));
    }

    var leftovers = [];
    Object.keys(FIELD_PLACEHOLDERS).forEach(function (ph) {
      if (new RegExp('\\b' + ph + '\\b').test(out)) { leftovers.push(ph); }
    });
    if (out.indexOf(EMAIL_PLACEHOLDER) !== -1) { leftovers.push(EMAIL_PLACEHOLDER); }

    // Nothing malformed leaves the browser.
    var parseError = '';
    if (!missing.length && !invalid.length && !leftovers.length) {
      try { JSON.parse(out); }
      catch (e) { parseError = String(e.message || e).slice(0, 200); }
    }

    return { text: out, missing: missing, invalid: invalid,
             leftovers: leftovers, parseError: parseError };
  }

  function bundleJobSpecNames(bundle) {
    var names = [];
    var res = (bundle && bundle.resources) || {};
    Object.keys(res).forEach(function (k) {
      if (res[k] && res[k].type === 'ZIS::JobSpec') { names.push(k); }
    });
    return names.sort();
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

    // 1. every substitution input present
    var sub = substitute(state.bundleText, s);
    if (sub.missing.length) {
      ok = false;
      steps.push({ ok: false, name: 'App settings complete',
                   detail: 'Not set: ' + sub.missing.join(', ') +
                           '. Set them in Admin Center > Apps > TSANet Connect.' });
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
        var conn = (s.tsanet_connection_name || CONN_PLACEHOLDER).trim();
        return req({ url: '/api/services/zis/connections/' + INTEGRATION + '?name=' + encodeURIComponent(conn), type: 'GET' })
          .then(function (r) {
            if (r.ok) { steps.push({ ok: true, name: 'TSANet connection "' + conn + '" exists' }); }
            else {
              steps.push({ ok: null, name: 'TSANet connection "' + conn + '" not confirmed',
                           detail: 'HTTP ' + r.status + '. The bundle will deploy, but TSANet actions fail auth ' +
                                   'if this connection is missing. Not blocking.' });
            }
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

    // 1. integration must exist before the bundle can be uploaded. Idempotent:
    //    an existing integration returns 200, a duplicate returns 409.
    return req({
      url: REGISTRY, type: 'POST', contentType: 'application/json',
      data: JSON.stringify({ description: 'TSANet Connect' })
    }).then(function (r) {
      var ok = r.ok || r.status === 409;
      record('Ensure ZIS integration "' + INTEGRATION + '"', ok,
             ok ? (r.status === 409 ? 'Already existed.' : 'Present.') : ('HTTP ' + r.status + ' ' + r.body));
      if (!ok) { throw new Error('integration'); }

      // 2. upload the substituted bundle
      return req({ url: REGISTRY + '/bundles', type: 'POST', contentType: 'application/json', data: sub.text });
    }).then(function (r) {
      record('Upload bundle', r.ok, r.ok ? 'Accepted.' : ('HTTP ' + r.status + ' ' + r.body));
      if (!r.ok) { throw new Error('upload'); }

      // 3. install EVERY job spec. Uploading orphaned them, so a spec skipped
      //    here stays dead. Attempt all, never abort on the first failure.
      var names = bundleJobSpecNames(state.bundle);
      return names.reduce(function (chain, name) {
        return chain.then(function () {
          return req({
            url: '/api/services/zis/registry/job_specs/install?job_spec_name=' +
                 encodeURIComponent(JOB_SPEC_PREFIX + name),
            type: 'POST', contentType: 'application/json'
          }).then(function (r2) {
            record('Install job spec ' + name, r2.ok, r2.ok ? '' : ('HTTP ' + r2.status + ' ' + r2.body));
          });
        });
      }, Promise.resolve());
    }).then(function () {
      return verify();
    }).catch(function () {
      return verify();   // still read back: a partial state must be reported accurately
    }).then(finish);
  }

  // Truth comes from the registry, not from the POST responses above.
  function verify() {
    var expected = bundleJobSpecNames(state.bundle);
    return req({ url: REGISTRY + '/job_specs', type: 'GET' }).then(function (r) {
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
        state.steps.push({ ok: null, name: 'Stale job specs installed from an older bundle',
                           detail: orphans.join(', ') + '. These still intercept events. ' +
                                   'Uninstall them with DELETE /api/services/zis/registry/job_specs/install' +
                                   '?job_spec_name=' + JOB_SPEC_PREFIX + '<name>' });
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
    resize();
  }

  function reportText() {
    var s = state.settings || {};
    var lines = [
      'TSANet Connect — ZIS deploy report',
      'when:        ' + new Date().toISOString(),
      'integration: ' + INTEGRATION,
      'env:         ' + (s.tsanet_env || '?'),
      'connection:  ' + (s.tsanet_connection_name || CONN_PLACEHOLDER),
      'bundle:      ' + (state.bundle && state.bundle.name) + ' / ' + (state.bundle && state.bundle.zis_template_version),
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
      return fetch('./tsanet_connect_bundle.json').then(function (r) { return r.text(); });
    }).then(function (text) {
      if (text == null) { return; }
      state.bundleText = text;
      state.bundle = JSON.parse(text);
      return client.metadata().then(function (md) {
        state.settings = (md && md.settings) || {};
        show('main', true);
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
  el('copy-btn').addEventListener('click', function () {
    var t = reportText();
    var done = function () { el('copy-note').textContent = ' copied'; };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(t).then(done, function () { window.prompt('Copy the report:', t); });
    } else { window.prompt('Copy the report:', t); }
  });

  boot();
}());
