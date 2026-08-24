"""The HTML surfaces: a shell that mounts the call UI, and the Settings
status panel.

The UI itself lives in ``ui/dist/call-agent.js`` — one hand-written ES
module with two entry points (``register`` for the SPA, ``mountCallUI`` for
this shell), so the window body and standalone mode cannot drift apart.

``/panel`` is **no longer how the app's window renders.** The window body is
``component`` mode now, because the mic could never work in the old iframe:
aw-workspace-ui builds that ``<iframe>`` with no ``allow`` attribute and the
panel is cross-origin to the SPA, so Permissions Policy denied the
microphone before any of this app's code ran. See the header comment in
``ui/dist/call-agent.js``.

This route stays for the surfaces that are genuinely a page: standalone mode
(``python -m call_agent_app``), and any browser you want to point straight
at the call. Top-level, so the mic works here too.

Note the shell serves HTML from an app route rather than ``ui/dist``: core's
``GET /api/apps/<slug>/ui/<path>`` sends everything that isn't ``.js`` as
``application/octet-stream``, so an ``index.html`` there downloads instead of
rendering. The ``.js`` module beside it is served correctly, which is why
only the markup lives here.
"""

from __future__ import annotations

PANEL_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Call Agent</title>
<style>html,body{height:100%}body{margin:0;background:#0e1116}#root{position:absolute;inset:0}</style>
</head>
<body>
<div id="root"></div>
<script type="module">
  // The page is served at <prefix>/panel, so "./ui/..." resolves to
  // <prefix>/ui/... — derived, never hardcoded, so this works behind the
  // workspace tunnel edge and in standalone mode unchanged.
  import { mountCallUI } from './ui/call-agent.js';
  const BASE = location.pathname.replace(/\/panel\/?$/, '');
  const q = location.search;
  mountCallUI(document.getElementById('root'), {
    apiUrl: (sub) => BASE + sub,
    // Carry the query string onto the socket: an identity token passed as
    // ?token= is how a non-browser/automated session authenticates a WS,
    // which browsers cannot do with a header.
    wsUrl: (sub) => (location.protocol === 'https:' ? 'wss:' : 'ws:')
      + '//' + location.host + BASE + sub + q,
    fetch: (sub, init) => fetch(BASE + sub, Object.assign({ credentials: 'same-origin' }, init)),
  });
</script>
</body>
</html>
"""


#: The Settings window's read-only half. The editable fields are core's own
#: config form (generated from ``config_schema``); what core cannot show is
#: whether the values actually resolve to a reachable platform — which is the
#: one question a broken call raises. So this panel answers exactly that, and
#: duplicates none of the form.
STATUS_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Call Agent — status</title>
<style>
  :root { --bg:#0e1116; --panel:#161b22; --line:#262d38; --text:#e6edf3; --muted:#8b949e; --good:#3fb950; --bad:#f85149; }
  body { margin:0; padding:12px; background:var(--bg); color:var(--text);
         font:13px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
  table { width:100%; border-collapse:collapse; background:var(--panel);
          border:1px solid var(--line); border-radius:10px; overflow:hidden; }
  th, td { text-align:left; padding:8px 12px; border-bottom:1px solid var(--line); vertical-align:top; }
  tr:last-child th, tr:last-child td { border-bottom:0; }
  th { color:var(--muted); font-weight:500; width:38%; white-space:nowrap; }
  code { background:#0d1117; padding:1px 5px; border-radius:4px; }
  .ok { color:var(--good); } .bad { color:var(--bad); }
</style>
</head>
<body>
<table id="t"><tr><td>loading…</td></tr></table>
<script>
(function () {
  var BASE = location.pathname.replace(/\/panel\/status\/?$/, '');
  function row(k, v, cls) {
    return '<tr><th>' + k + '</th><td' + (cls ? ' class="' + cls + '"' : '') + '>' + v + '</td></tr>';
  }
  fetch(BASE + '/settings', { credentials: 'same-origin' })
    .then(function (r) { return r.json(); })
    .then(function (s) {
      var html = ''
        + row('Agent', '<code>' + s.agent_slug + '</code>')
        + row('Conversation target', '<code>' + s.target_slug + '</code>')
        + row('Voice language', s.default_voice_lang)
        + row('agents-platform', s.agents_platform_base
              ? '<code>' + s.agents_platform_base + '</code>' : 'not set',
              s.agents_platform_base ? '' : 'bad')
        + row('Identity token', s.has_token ? 'present' : 'missing',
              s.has_token ? 'ok' : 'bad')
        + row('Credentials from', '<code>' + s.credentials_source + '</code>')
        + row('Turn timeout', s.max_poll_seconds + 's (polled every '
              + s.poll_interval_seconds + 's)');
      var tel = s.telephony || {};
      html += row('SIP telephony', tel.enabled ? 'enabled' : 'disabled', tel.enabled ? 'ok' : '')
        + row('SIP provider', '<code>' + (tel.provider || 'zadarma') + '</code>')
        + row('Portuguese number', tel.public_number || 'not set', tel.public_number ? '' : 'bad')
        + row('SIP credentials', tel.configured ? 'present' : 'missing: ' + (tel.missing || []).join(', '),
              tel.configured ? 'ok' : 'bad');
      document.getElementById('t').innerHTML = html
        + row('Platform reachable', '<span id="reach">checking…</span>')
        + row('Asterisk', '<span id="ast">checking…</span>');
      fetch(BASE + '/telephony/status', { credentials: 'same-origin' })
        .then(function (r) { return r.json(); })
        .then(function (t) {
          var el = document.getElementById('ast');
          var ok = !!(t.asterisk && t.asterisk.reachable);
          el.textContent = ok ? 'reachable' : (t.enabled ? 'not reachable' : 'disabled');
          el.className = ok ? 'ok' : (t.enabled ? 'bad' : '');
        });
      return fetch(BASE + '/agents-list', { credentials: 'same-origin' })
        .then(function (r) { return r.json(); })
        .then(function (a) {
          var n = (a.agents || []).length;
          var el = document.getElementById('reach');
          // The fallback row is the agent slug echoed back, so exactly one row
          // whose slug equals the configured agent means the proxy fell back
          // rather than actually reaching anything.
          var real = n > 1 || (n === 1 && a.agents[0].slug !== s.agent_slug);
          el.textContent = real ? 'yes — ' + n + ' agents' : 'no — could not list agents';
          el.className = real ? 'ok' : 'bad';
        });
    })
    .catch(function (e) {
      document.getElementById('t').innerHTML =
        '<tr><td class="bad">could not load settings: ' + e.message + '</td></tr>';
    });
})();
</script>
</body>
</html>
"""
