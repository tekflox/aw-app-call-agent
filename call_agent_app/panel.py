"""The call UI, as one self-contained HTML document.

Served from ``GET /api/apps/call-agent/panel`` and framed by the app's
window. It is deliberately NOT shipped as a ``ui/dist`` bundle: core's
``GET /api/apps/<slug>/ui/<path>`` serves everything that isn't ``.js`` as
``application/octet-stream``, so an ``index.html`` there downloads instead of
rendering. A route returning ``HTMLResponse`` is the shape that actually
works in a window (same conclusion aw-app-remote-screen reached for its own
panels), and it costs no build step and no ``ui:code`` grant.

No framework, no bundler, no network imports — a call UI that needs a CDN to
render is a call UI that fails on a workspace behind a proxy.

Speech in is the browser's own ``SpeechRecognition`` (Chrome/Edge). Where it
is missing — Firefox, and any non-HTTPS origin — the text box below is the
whole UI and everything else still works; the call is a WebSocket carrying
text either way, exactly as the iOS client uses it.
"""

from __future__ import annotations

PANEL_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Call Agent</title>
<style>
  :root {
    --bg: #0e1116; --panel: #161b22; --line: #262d38; --text: #e6edf3;
    --muted: #8b949e; --accent: #2f81f7; --good: #3fb950; --bad: #f85149;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0; padding: 12px; background: var(--bg); color: var(--text);
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    display: flex; flex-direction: column; gap: 12px;
  }
  .bar {
    display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
    background: var(--panel); border: 1px solid var(--line);
    border-radius: 10px; padding: 10px 12px;
  }
  .bar label { color: var(--muted); font-size: 12px; }
  select, input[type=text] {
    background: #0d1117; color: var(--text); border: 1px solid var(--line);
    border-radius: 8px; padding: 7px 9px; font: inherit; min-width: 0;
  }
  select:focus, input:focus { outline: 1px solid var(--accent); }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--muted); flex: none; }
  .dot.on { background: var(--good); box-shadow: 0 0 8px var(--good); }
  .dot.busy { background: #d29922; }
  .dot.err { background: var(--bad); }
  #state { color: var(--muted); font-size: 12px; }
  .spacer { flex: 1; }
  button {
    background: #21262d; color: var(--text); border: 1px solid var(--line);
    border-radius: 8px; padding: 8px 14px; font: inherit; cursor: pointer;
  }
  button:hover:not(:disabled) { border-color: var(--accent); }
  button:disabled { opacity: .45; cursor: default; }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff; font-weight: 600; }
  button.danger { background: var(--bad); border-color: var(--bad); color: #fff; font-weight: 600; }
  button.on { border-color: var(--good); color: var(--good); }
  /* Icons are inline SVG, never emoji: the containers this renders in ship no
     emoji font, so 📞/🔊 come out as empty boxes. */
  button svg { width: 15px; height: 15px; vertical-align: -2px; margin-right: 6px; fill: none;
               stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
  #log {
    flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 10px;
    background: var(--panel); border: 1px solid var(--line);
    border-radius: 10px; padding: 14px; min-height: 140px;
  }
  .msg { max-width: 82%; padding: 9px 13px; border-radius: 14px; white-space: pre-wrap; word-wrap: break-word; }
  .msg.me { align-self: flex-end; background: var(--accent); color: #fff; border-bottom-right-radius: 4px; }
  .msg.agent { align-self: flex-start; background: #0d1117; border: 1px solid var(--line); border-bottom-left-radius: 4px; }
  .msg.sys { align-self: center; background: transparent; color: var(--muted); font-size: 12px; padding: 2px; }
  .msg.err { align-self: center; background: rgba(248,81,73,.12); color: var(--bad); border: 1px solid rgba(248,81,73,.35); font-size: 13px; }
  .row { display: flex; gap: 8px; }
  .row input { flex: 1; }
  .hint { color: var(--muted); font-size: 12px; }
</style>
</head>
<body>
  <div class="bar">
    <span id="dot" class="dot"></span>
    <span id="state">idle</span>
    <span class="spacer"></span>
    <label for="agent">agent</label>
    <select id="agent"></select>
    <button id="speak" class="on" title="Speak the agent's replies out loud"></button>
    <button id="clear" title="Forget the conversation and start a fresh one">new</button>
  </div>

  <div id="log">
    <div class="msg sys">Not in a call.</div>
  </div>

  <div class="row">
    <button id="call" class="primary"></button>
    <input id="text" type="text" placeholder="…or type a message and press Enter" disabled />
    <button id="send" disabled>Send</button>
  </div>
  <div class="hint" id="hint"></div>

<script>
(function () {
  // The page is served at <prefix>/panel, so <prefix> is where every other
  // route of this app lives — in integrated mode that is
  // /api/apps/call-agent, in standalone the same. Derived, never hardcoded,
  // so the panel works unchanged behind the workspace's tunnel edge.
  var BASE = location.pathname.replace(/\/panel\/?$/, '');
  var WS_URL = (location.protocol === 'https:' ? 'wss:' : 'ws:')
    + '//' + location.host + BASE + '/ws/call' + location.search;

  var el = function (id) { return document.getElementById(id); };
  var log = el('log'), dot = el('dot'), state = el('state');
  var callBtn = el('call'), sendBtn = el('send'), textIn = el('text');
  var agentSel = el('agent'), speakBtn = el('speak'), clearBtn = el('clear');

  // Inline SVG, not emoji — see the stylesheet note.
  var ICON = {
    phone: '<svg viewBox="0 0 24 24"><path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .3 1.9.6 2.8a2 2 0 0 1-.4 2.1L8 9.9a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.5 2.8.6a2 2 0 0 1 1.8 2.1z"/></svg>',
    hangup: '<svg viewBox="0 0 24 24"><rect x="5" y="5" width="14" height="14" rx="2"/></svg>',
    on: '<svg viewBox="0 0 24 24"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M18.5 5.5a9 9 0 0 1 0 13"/></svg>',
    off: '<svg viewBox="0 0 24 24"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><line x1="22" y1="9" x2="16" y2="15"/><line x1="16" y1="9" x2="22" y2="15"/></svg>'
  };

  var ws = null, inCall = false, speak = true, streaming = null;
  var lang = navigator.language || 'pt-BR';
  var audio = new Audio();
  var recog = null, wantMic = false;

  function setState(text, cls) {
    state.textContent = text;
    dot.className = 'dot' + (cls ? ' ' + cls : '');
  }

  function add(cls, text) {
    var first = log.querySelector('.msg.sys');
    if (first && first.textContent === 'Not in a call.') first.remove();
    var d = document.createElement('div');
    d.className = 'msg ' + cls;
    d.textContent = text;
    log.appendChild(d);
    log.scrollTop = log.scrollHeight;
    return d;
  }

  // ---- text-to-speech -----------------------------------------------------
  // Server-side Edge TTS, same endpoint (and same raw MP3) the iOS client
  // uses — not the browser's speechSynthesis, so every surface of this app
  // sounds identical.
  function say(text) {
    if (!speak || !text) { restartMic(); return; }
    audio.src = BASE + '/tts?lang=' + encodeURIComponent(lang)
      + '&text=' + encodeURIComponent(text.slice(0, 1200));
    audio.onended = restartMic;
    audio.onerror = restartMic;
    audio.play().catch(function () { restartMic(); });
  }

  // ---- speech-to-text -----------------------------------------------------
  var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (SR) {
    recog = new SR();
    recog.lang = lang;
    recog.continuous = false;
    recog.interimResults = false;
    recog.onresult = function (e) {
      var said = e.results[0][0].transcript.trim();
      if (said) send(said);
    };
    recog.onerror = function (e) {
      if (e.error === 'not-allowed') {
        wantMic = false;
        add('err', 'Microphone blocked — allow it in the browser, or just type.');
      }
    };
    recog.onend = function () { if (wantMic && inCall && !streaming) startMic(); };
    el('hint').textContent = 'Speak after "listening", or type. The reply is streamed and spoken back.';
  } else {
    el('hint').textContent = 'This browser has no speech recognition — type instead. '
      + 'Replies are still spoken back to you.';
  }

  function startMic() {
    if (!recog || !inCall) return;
    try { recog.start(); setState('listening', 'on'); } catch (err) { /* already started */ }
  }
  function stopMic() { wantMic = false; if (recog) { try { recog.stop(); } catch (err) {} } }
  function restartMic() {
    if (inCall && wantMic && !streaming) startMic();
    else if (inCall) setState('ready', 'on');
  }

  // ---- the call ----------------------------------------------------------
  function hangUp(reason) {
    inCall = false;
    stopMic();
    if (ws) { try { ws.close(); } catch (e) {} ws = null; }
    callBtn.innerHTML = ICON.phone + 'Call';
    callBtn.className = 'primary';
    textIn.disabled = true; sendBtn.disabled = true;
    setState(reason || 'idle', reason === 'error' ? 'err' : '');
  }

  function connect() {
    setState('connecting…', 'busy');
    ws = new WebSocket(WS_URL);

    ws.onopen = function () { inCall = true; };

    ws.onmessage = function (ev) {
      var m;
      try { m = JSON.parse(ev.data); } catch (e) { return; }

      if (m.type === 'ready') {
        callBtn.innerHTML = ICON.hangup + 'Hang up';
        callBtn.className = 'danger';
        textIn.disabled = false; sendBtn.disabled = false;
        add('sys', 'Connected to ' + m.agent
          + (m.resumed ? ' — resuming your last conversation.' : ' — new conversation.'));
        setState('ready', 'on');
        wantMic = !!recog;
        startMic();
        textIn.focus();

      } else if (m.type === 'heartbeat') {
        if (streaming) setState('thinking…', 'busy');

      } else if (m.type === 'text_delta') {
        if (!streaming) { streaming = add('agent', ''); }
        streaming.textContent += m.text;
        log.scrollTop = log.scrollHeight;
        setState('answering…', 'busy');

      } else if (m.type === 'done') {
        if (!streaming) streaming = add('agent', '');
        // The streamed deltas are the authority when there were any; `text`
        // is the whole reply, which is all we get for an agent whose runner
        // emits no llm_token events at all.
        if (!streaming.textContent) streaming.textContent = m.text;
        var spoken = streaming.textContent;
        streaming = null;
        setState('ready', 'on');
        say(spoken);

      } else if (m.type === 'cleared') {
        add('sys', 'Conversation reset — the next thing you say starts fresh.');

      } else if (m.type === 'error') {
        streaming = null;
        add('err', m.message || 'unknown error');
        setState('error', 'err');
      }
    };

    ws.onclose = function () { if (inCall) hangUp('disconnected'); };
    ws.onerror = function () { setState('connection failed', 'err'); };
  }

  function send(text) {
    if (!ws || ws.readyState !== 1) return;
    add('me', text);
    stopMicForTurn();
    setState('thinking…', 'busy');
    ws.send(JSON.stringify({ type: 'message', text: text }));
  }

  function stopMicForTurn() {
    // Stop listening while the agent answers, or the TTS playback feeds
    // straight back into the recogniser and the call talks to itself.
    if (recog) { try { recog.stop(); } catch (e) {} }
  }

  callBtn.innerHTML = ICON.phone + 'Call';
  speakBtn.innerHTML = ICON.on + 'voice on';

  callBtn.onclick = function () { if (inCall) hangUp('idle'); else connect(); };
  sendBtn.onclick = function () {
    var t = textIn.value.trim();
    if (t) { textIn.value = ''; send(t); }
  };
  textIn.onkeydown = function (e) { if (e.key === 'Enter') sendBtn.onclick(); };
  speakBtn.onclick = function () {
    speak = !speak;
    speakBtn.innerHTML = (speak ? ICON.on : ICON.off) + (speak ? 'voice on' : 'voice off');
    speakBtn.className = speak ? 'on' : '';
    if (!speak) { audio.pause(); restartMic(); }
  };
  clearBtn.onclick = function () {
    if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: 'clear' }));
    else add('sys', 'Start a call first.');
  };

  // The picker is informational until a call is placed: which agent a call
  // reaches is workspace config (Settings), not a per-visitor choice, so this
  // shows what is configured and what else exists without silently letting
  // one browser tab re-point everyone else's calls.
  fetch(BASE + '/settings', { credentials: 'same-origin' })
    .then(function (r) { return r.json(); })
    .then(function (s) {
      if (s.default_voice_lang && !navigator.language) lang = s.default_voice_lang;
      return fetch(BASE + '/agents-list', { credentials: 'same-origin' })
        .then(function (r) { return r.json(); })
        .then(function (a) {
          (a.agents || []).forEach(function (row) {
            var o = document.createElement('option');
            o.value = row.slug; o.textContent = row.name || row.slug;
            agentSel.appendChild(o);
          });
          agentSel.value = s.agent_slug;
          agentSel.disabled = true;
          agentSel.title = 'Set in this app’s Settings (target: ' + s.target_slug + ')';
          if (!s.agents_platform_base || !s.has_token) {
            add('err', 'No agents-platform credentials configured — open Settings.');
          }
        });
    })
    .catch(function (e) { add('err', 'Could not load settings: ' + e.message); });
})();
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
      document.getElementById('t').innerHTML = html
        + row('Platform reachable', '<span id="reach">checking…</span>');
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
