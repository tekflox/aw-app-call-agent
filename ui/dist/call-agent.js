// The Call Agent UI — one hand-written ES module, two entry points.
//
//   register(host)          component-mode window body, loaded into the SPA
//   mountCallUI(root, io)   the same UI mounted into any DOM node (the
//                           /panel HTML shell, standalone mode)
//
// Committed as-is under ui/dist/ with **no build step**: there is no JSX and
// no npm dependency here (React comes from `host.React`, never imported —
// the one-React-instance invariant), so a bundler would only add a way for
// the shipped file to drift from the source. Core serves this path as
// text/javascript.
//
// ---------------------------------------------------------------------------
// Why component mode exists at all, and why the iframe could not stay
// ---------------------------------------------------------------------------
// The first version rendered the panel through the declarative `iframe`
// widget. The microphone never worked, and no amount of app-side code could
// have fixed it: aw-workspace-ui's AppWindow.jsx builds that <iframe> with a
// `sandbox` attribute and **no `allow`**, and the panel is served from the
// API host (`api.<ws>.workspace.…`) while the SPA runs on `<ws>.workspace.…`.
// A cross-origin frame with no `allow="microphone"` is denied the mic by
// Permissions Policy before any script runs — `SpeechRecognition` fails with
// `not-allowed` and the browser never even offers a prompt. Verified live in
// the real DOM 2026-08-15: `allow` came back `null`.
//
// Rendering into the SPA's own document sidesteps it entirely — the mic is
// then an ordinary top-level permission on the origin the user already
// trusts. (Core gaining an `allow` passthrough on that widget would be a
// worthwhile fix for every other app, but it is not this app's to make.)
// ---------------------------------------------------------------------------

const SLUG = 'call-agent';

/* ========================================================================
 * The orb
 * ====================================================================== */

// A blob whose radius wobbles on a few sine harmonics and swells with the
// audio level. Ported in spirit from the standalone aw-call-agent's frontend
// — the moving ball Frederico asked for by name. That repo is gone (only
// aw-app-call-agent survives), so this is a rebuild, not a copy.
//
// The point of it is to be an honest indicator, not decoration: every visible
// state maps to a real one. Idle breathes, listening tracks YOUR voice,
// thinking spins with no level input (the agent is quiet on purpose during
// tool calls), speaking tracks the reply audio. A user who can see which of
// those four it is never has to guess whether a silent call is broken.
const PALETTE = {
  idle:      { core: '#3d4756', glow: '#2f81f7', speed: 0.35, base: 0.78 },
  listening: { core: '#3fb950', glow: '#3fb950', speed: 1.10, base: 0.86 },
  thinking:  { core: '#d29922', glow: '#d29922', speed: 2.20, base: 0.82 },
  speaking:  { core: '#2f81f7', glow: '#58a6ff', speed: 1.60, base: 0.90 },
  error:     { core: '#f85149', glow: '#f85149', speed: 0.30, base: 0.74 },
};

function createOrb(canvas) {
  const ctx = canvas.getContext('2d');
  let state = 'idle';
  let level = 0;      // 0..1, smoothed
  let target = 0;
  let t = 0;
  let raf = null;
  let dead = false;

  function resize() {
    const dpr = window.devicePixelRatio || 1;
    const r = canvas.getBoundingClientRect();
    if (!r.width || !r.height) return;
    canvas.width = Math.round(r.width * dpr);
    canvas.height = Math.round(r.height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function frame() {
    if (dead) return;
    const r = canvas.getBoundingClientRect();
    const w = r.width, h = r.height;
    if (w && h) {
      const p = PALETTE[state] || PALETTE.idle;
      // Ease toward the target so a spiky RMS reads as motion, not jitter.
      level += (target - level) * 0.18;
      t += 0.016 * p.speed;

      ctx.clearRect(0, 0, w, h);
      const cx = w / 2, cy = h / 2;
      const unit = Math.min(w, h) / 2;
      const breathe = 1 + Math.sin(t * 0.9) * 0.035;
      const base = unit * 0.46 * p.base * breathe * (1 + level * 0.42);

      // Outer glow — drawn first so the body sits on top of it.
      const glow = ctx.createRadialGradient(cx, cy, base * 0.4, cx, cy, base * 2.3);
      glow.addColorStop(0, hexA(p.glow, 0.30 + level * 0.34));
      glow.addColorStop(1, hexA(p.glow, 0));
      ctx.fillStyle = glow;
      ctx.beginPath();
      ctx.arc(cx, cy, base * 2.3, 0, Math.PI * 2);
      ctx.fill();

      // The blob. Three harmonics at incommensurate speeds so the outline
      // never visibly repeats.
      ctx.beginPath();
      const STEPS = 96;
      for (let i = 0; i <= STEPS; i++) {
        const a = (i / STEPS) * Math.PI * 2;
        const wob =
          Math.sin(a * 3 + t * 1.7) * (0.045 + level * 0.10) +
          Math.sin(a * 5 - t * 1.1) * (0.030 + level * 0.07) +
          Math.sin(a * 2 + t * 0.6) * 0.022;
        const rad = base * (1 + wob);
        const x = cx + Math.cos(a) * rad;
        const y = cy + Math.sin(a) * rad;
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.closePath();
      const body = ctx.createRadialGradient(
        cx - base * 0.3, cy - base * 0.35, base * 0.1, cx, cy, base * 1.25);
      body.addColorStop(0, hexA(p.glow, 0.95));
      body.addColorStop(0.55, hexA(p.core, 0.92));
      body.addColorStop(1, hexA(p.core, 0.55));
      ctx.fillStyle = body;
      ctx.fill();

      // Thinking has no audio to track, so it gets an orbiting highlight —
      // otherwise a long tool-call phase looks identical to a frozen UI.
      if (state === 'thinking') {
        const oa = t * 1.9;
        ctx.beginPath();
        ctx.arc(cx + Math.cos(oa) * base * 1.45, cy + Math.sin(oa) * base * 1.45,
                Math.max(2, unit * 0.030), 0, Math.PI * 2);
        ctx.fillStyle = hexA(p.glow, 0.85);
        ctx.fill();
      }
    }
    raf = requestAnimationFrame(frame);
  }

  resize();
  raf = requestAnimationFrame(frame);
  const onResize = () => resize();
  window.addEventListener('resize', onResize);

  return {
    setState(s) { state = s; },
    setLevel(v) { target = Math.max(0, Math.min(1, v)); },
    resize,
    destroy() {
      dead = true;
      if (raf) cancelAnimationFrame(raf);
      window.removeEventListener('resize', onResize);
    },
  };
}

function hexA(hex, a) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

/* ========================================================================
 * Audio levels
 * ====================================================================== */

// One AudioContext for the whole UI. Browsers cap how many a page may open,
// and a call that hangs up and redials would otherwise leak one per attempt.
function createMeter() {
  let ac = null;
  let micNode = null, micStream = null;
  let ttsNode = null;
  let analyser = null;

  function ensure() {
    if (!ac) ac = new (window.AudioContext || window.webkitAudioContext)();
    if (ac.state === 'suspended') ac.resume().catch(() => {});
    if (!analyser) {
      analyser = ac.createAnalyser();
      analyser.fftSize = 512;
      analyser.smoothingTimeConstant = 0.6;
    }
    return ac;
  }

  return {
    /** Open the mic. Resolves to null on success, or an error string. */
    async openMic() {
      try {
        ensure();
        micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
        micNode = ac.createMediaStreamSource(micStream);
        micNode.connect(analyser);
        return null;
      } catch (e) {
        return `${e.name}: ${e.message}`;
      }
    },
    closeMic() {
      try { if (micNode) micNode.disconnect(); } catch (e) { /* already gone */ }
      if (micStream) micStream.getTracks().forEach((tr) => tr.stop());
      micNode = null; micStream = null;
    },
    /** Route a played <audio> through the analyser so the orb tracks the
     *  reply. Only safe because the src is a blob: URL we fetched ourselves —
     *  a cross-origin media element would taint the graph and read as
     *  silence. */
    attachAudio(el) {
      try {
        ensure();
        if (!ttsNode) {
          ttsNode = ac.createMediaElementSource(el);
          ttsNode.connect(analyser);
          ttsNode.connect(ac.destination);
        }
      } catch (e) { /* already attached, or unsupported — orb falls back */ }
    },
    /** RMS of the current window, roughly normalised to 0..1. */
    level() {
      if (!analyser) return 0;
      const buf = new Uint8Array(analyser.fftSize);
      analyser.getByteTimeDomainData(buf);
      let sum = 0;
      for (let i = 0; i < buf.length; i++) {
        const v = (buf[i] - 128) / 128;
        sum += v * v;
      }
      return Math.min(1, Math.sqrt(sum / buf.length) * 3.2);
    },
    destroy() {
      this.closeMic();
      try { if (ac) ac.close(); } catch (e) { /* already closed */ }
      ac = null; analyser = null; ttsNode = null;
    },
  };
}

/* ========================================================================
 * Markup + styles
 * ====================================================================== */

const CSS = `
.cag-root{position:absolute;inset:0;display:flex;flex-direction:column;gap:10px;
  padding:12px;background:#0e1116;color:#e6edf3;overflow:hidden;
  font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;box-sizing:border-box}
.cag-root *{box-sizing:border-box}
.cag-bar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;background:#161b22;
  border:1px solid #262d38;border-radius:10px;padding:8px 12px;flex:none}
.cag-bar label{color:#8b949e;font-size:12px}
.cag-root select,.cag-root input[type=text]{background:#0d1117;color:#e6edf3;border:1px solid #262d38;
  border-radius:8px;padding:7px 9px;font:inherit;min-width:0}
.cag-dot{width:9px;height:9px;border-radius:50%;background:#8b949e;flex:none}
.cag-dot.on{background:#3fb950;box-shadow:0 0 8px #3fb950}
.cag-dot.busy{background:#d29922}
.cag-dot.err{background:#f85149}
.cag-state{color:#8b949e;font-size:12px;min-width:74px}
.cag-sp{flex:1}
.cag-root button{background:#21262d;color:#e6edf3;border:1px solid #262d38;border-radius:8px;
  padding:8px 14px;font:inherit;cursor:pointer;display:inline-flex;align-items:center;gap:6px}
.cag-root button:hover:not(:disabled){border-color:#2f81f7}
.cag-root button:disabled{opacity:.45;cursor:default}
.cag-root button.primary{background:#2f81f7;border-color:#2f81f7;color:#fff;font-weight:600}
.cag-root button.danger{background:#f85149;border-color:#f85149;color:#fff;font-weight:600}
.cag-root button.on{border-color:#3fb950;color:#3fb950}
.cag-root button svg{width:15px;height:15px;fill:none;stroke:currentColor;stroke-width:2;
  stroke-linecap:round;stroke-linejoin:round}
.cag-stage{flex:1;display:flex;min-height:0;gap:12px}
.cag-orbwrap{flex:none;width:38%;min-width:190px;position:relative;display:flex;
  align-items:center;justify-content:center}
.cag-orb{width:100%;height:100%;display:block}
.cag-caption{position:absolute;bottom:6px;left:0;right:0;text-align:center;color:#8b949e;font-size:12px}
.cag-log{flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:10px;background:#161b22;
  border:1px solid #262d38;border-radius:10px;padding:14px;min-width:0}
.cag-msg{max-width:88%;padding:9px 13px;border-radius:14px;white-space:pre-wrap;word-wrap:break-word}
.cag-msg.me{align-self:flex-end;background:#2f81f7;color:#fff;border-bottom-right-radius:4px}
.cag-msg.agent{align-self:flex-start;background:#0d1117;border:1px solid #262d38;border-bottom-left-radius:4px}
.cag-msg.sys{align-self:center;background:transparent;color:#8b949e;font-size:12px;padding:2px;text-align:center}
.cag-msg.err{align-self:center;background:rgba(248,81,73,.12);color:#f85149;
  border:1px solid rgba(248,81,73,.35);font-size:13px}
.cag-row{display:flex;gap:8px;flex:none}
.cag-row input{flex:1}
.cag-hint{color:#8b949e;font-size:12px;flex:none}
.cag-diag{flex:none;max-height:150px;overflow:auto;margin:0;padding:10px 12px;background:#0d1117;
  border:1px solid #262d38;border-radius:8px;color:#8b949e;font:11px/1.45 ui-monospace,SFMono-Regular,
  Menlo,monospace;white-space:pre-wrap;user-select:text}
@media (max-width:640px){.cag-stage{flex-direction:column}.cag-orbwrap{width:100%;height:38%;min-width:0}}
`;

const ICON = {
  phone: '<svg viewBox="0 0 24 24"><path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .3 1.9.6 2.8a2 2 0 0 1-.4 2.1L8 9.9a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.5 2.8.6a2 2 0 0 1 1.8 2.1z"/></svg>',
  // Filled — a stroked square reads as exactly the tofu box these SVGs exist
  // to avoid (the container fonts ship no emoji glyphs).
  hangup: '<svg viewBox="0 0 24 24"><rect x="5" y="5" width="14" height="14" rx="2" fill="currentColor" stroke="none"/></svg>',
  on: '<svg viewBox="0 0 24 24"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M18.5 5.5a9 9 0 0 1 0 13"/></svg>',
  off: '<svg viewBox="0 0 24 24"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><line x1="22" y1="9" x2="16" y2="15"/><line x1="16" y1="9" x2="22" y2="15"/></svg>',
};

const HTML = `
<div class="cag-bar">
  <span class="cag-dot" data-el="dot"></span>
  <span class="cag-state" data-el="state">idle</span>
  <span class="cag-sp"></span>
  <label>agent</label>
  <select data-el="agent"></select>
  <button class="on" data-el="speak" title="Speak the agent's replies out loud"></button>
  <button data-el="diag" title="What speech recognition is actually doing">diag</button>
  <button data-el="clear" title="Forget the conversation and start a fresh one">new</button>
</div>
<pre class="cag-diag" data-el="diagpanel" hidden></pre>
<div class="cag-stage">
  <div class="cag-orbwrap">
    <canvas class="cag-orb" data-el="orb"></canvas>
    <div class="cag-caption" data-el="caption">not in a call</div>
  </div>
  <div class="cag-log" data-el="log"><div class="cag-msg sys">Not in a call.</div></div>
</div>
<div class="cag-row">
  <button class="primary" data-el="call"></button>
  <input data-el="text" type="text" placeholder="…or type a message and press Enter" disabled />
  <button data-el="send" disabled>Send</button>
</div>
<div class="cag-hint" data-el="hint"></div>
`;

/* ========================================================================
 * mountCallUI
 * ====================================================================== */

export function mountCallUI(root, io) {
  if (!document.getElementById('cag-style')) {
    const st = document.createElement('style');
    st.id = 'cag-style';
    st.textContent = CSS;
    document.head.appendChild(st);
  }

  const host = document.createElement('div');
  host.className = 'cag-root';
  host.innerHTML = HTML;
  root.appendChild(host);
  const el = (n) => host.querySelector(`[data-el="${n}"]`);

  const dot = el('dot'), stateEl = el('state'), logEl = el('log'), caption = el('caption');
  const callBtn = el('call'), sendBtn = el('send'), textIn = el('text');
  const agentSel = el('agent'), speakBtn = el('speak'), clearBtn = el('clear');

  const orb = createOrb(el('orb'));
  const meter = createMeter();

  let ws = null, inCall = false, speak = true, streaming = null, dead = false;
  // `streaming` holds the DOM node the reply is being written into, so it is
  // null from the moment a turn is sent until the FIRST token arrives, and
  // null again from `done` through the whole spoken reply. Using it as "is a
  // turn in flight" — which this file did — meant the recogniser restarted
  // while the agent was still thinking and again while its reply was being
  // spoken, so the call listened to its own voice. Turn state gets its own
  // flag; the node stays a node.
  let turnInFlight = false;
  let lang = navigator.language || 'pt-BR';
  let recog = null, wantMic = false, micReady = false;
  // Transcription health.
  //
  // `SpeechRecognition` can be present, accept `start()` without throwing,
  // and then emit **nothing at all** — no `start`, no `error`, no `end`,
  // forever. Measured 2026-08-15 on the workspace's own Chromium against a
  // fake audio device fed real looping speech: 30 seconds, zero events, while
  // `getUserMedia` held a live track and the orb tracked the waveform the
  // whole time. That is a browser with no speech backend, and it is
  // indistinguishable from a dead microphone to anyone watching the screen —
  // the UI just says "listening" and never moves. Exactly the symptom this
  // app was already debugged for once.
  //
  // So the detector is a **clock**, not an event counter: an event-based
  // check cannot fire in a browser that emits no events. `silentEnds` is kept
  // as a faster secondary trigger for browsers that do cycle properly but
  // return nothing.
  let diagOpen = false, meterReleased = false;

  // Every SpeechRecognition event, with timings — the only way to tell the
  // failure modes apart, and they are invisible otherwise. `no-speech` while
  // the meter hears a voice means the recogniser is not getting the audio;
  // `network`/`service-not-allowed` mean the cloud side refused; no events at
  // all mean no engine. Surfaced through the "diag" button so a user can hand
  // back a fact instead of "it doesn't work".
  const srLog = [];
  let srT0 = 0, lastSrError = '';
  function srNote(name, extra) {
    srLog.push({ ev: name, ms: srT0 ? Date.now() - srT0 : 0, extra: extra || '' });
    if (srLog.length > 40) srLog.shift();
    if (diagOpen) renderDiag();
  }

  const NO_TRANSCRIPT_AFTER_MS = 12000;   // heard a voice this long, got no text
  const AUDIBLE = 0.06;                   // level that counts as "someone spoke"
  let peakSinceResult = 0, silentEnds = 0, gotResultThisRun = false;
  let warnedNoTranscript = false, listeningSince = 0;
  const audio = new Audio();
  audio.crossOrigin = 'anonymous';
  let levelTimer = null;

  callBtn.innerHTML = ICON.phone + 'Call';
  speakBtn.innerHTML = ICON.on + 'voice on';

  function setState(text, cls, orbState) {
    stateEl.textContent = text;
    dot.className = 'cag-dot' + (cls ? ' ' + cls : '');
    if (orbState) orb.setState(orbState);
    caption.textContent = text;
  }

  function add(cls, text) {
    const first = logEl.querySelector('.cag-msg.sys');
    if (first && first.textContent === 'Not in a call.') first.remove();
    const d = document.createElement('div');
    d.className = 'cag-msg ' + cls;
    d.textContent = text;
    logEl.appendChild(d);
    logEl.scrollTop = logEl.scrollHeight;
    return d;
  }

  // ---- audio out --------------------------------------------------------
  // Fetched through io.fetch (not assigned straight to audio.src) for two
  // reasons: the endpoint is identity-gated, and a blob: URL is same-origin,
  // which is what lets the orb read the waveform at all.
  async function say(text) {
    if (!speak || !text) { restartMic(); return; }
    try {
      const resp = await io.fetch(
        `/tts?lang=${encodeURIComponent(lang)}&text=${encodeURIComponent(text.slice(0, 1200))}`);
      if (!resp.ok) throw new Error(`tts ${resp.status}`);
      const url = URL.createObjectURL(await resp.blob());
      audio.src = url;
      meter.attachAudio(audio);
      setState('speaking', 'busy', 'speaking');
      audio.onended = () => { URL.revokeObjectURL(url); restartMic(); };
      audio.onerror = () => { URL.revokeObjectURL(url); restartMic(); };
      await audio.play();
    } catch (e) {
      // Never let a TTS failure strand the call — the text is already on
      // screen, so just go back to listening.
      restartMic();
    }
  }

  // ---- speech in --------------------------------------------------------
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (SR) {
    recog = new SR();
    recog.lang = lang;
    recog.continuous = false;
    recog.interimResults = false;
    ['start', 'audiostart', 'soundstart', 'speechstart', 'speechend',
     'soundend', 'audioend', 'nomatch'].forEach((n) => {
      recog['on' + n] = () => srNote(n);
    });
    recog.onresult = (e) => {
      const said = e.results[0][0].transcript.trim();
      srNote('result', said.slice(0, 60));
      gotResultThisRun = true;
      silentEnds = 0;
      peakSinceResult = 0;
      listeningSince = Date.now();
      if (said) send(said);
    };
    recog.onerror = (e) => {
      lastSrError = e.error;
      srNote('error', e.error);
      if (e.error === 'not-allowed') {
        wantMic = false;
        add('err', 'Microphone blocked. Allow it for this site and press Call again — '
          + 'or just type below, it works the same.');
        setState('mic blocked', 'err', 'error');
      } else if (e.error === 'network' || e.error === 'service-not-allowed') {
        // Distinct from "blocked": the mic is fine, the speech-to-text
        // *service* is unreachable. Chrome hands audio to Google to
        // transcribe; a build without that backend (plain Chromium, some
        // enterprise/offline setups) lands here.
        wantMic = false;
        noTranscriptWarning('this browser could not reach a speech-recognition service');
      }
      // 'no-speech' and 'aborted' are ordinary — silence, or our own stop()
      // between turns. Counting them would cry wolf on every quiet moment.
    };
    recog.onend = () => {
      srNote('end');
      if (!gotResultThisRun && peakSinceResult > AUDIBLE) silentEnds++;
      else if (gotResultThisRun) silentEnds = 0;
      gotResultThisRun = false;
      peakSinceResult = 0;
      // Three consecutive rounds where the meter clearly heard you and the
      // recogniser returned nothing is not bad luck.
      if (silentEnds >= 3) {
        wantMic = false;
        noTranscriptWarning('your voice is coming through, but this browser '
          + 'is not turning it into text');
        return;
      }
      if (wantMic && inCall && !turnInFlight) startMic();
    };
    el('hint').textContent = 'Speak after "listening", or type. The reply is streamed and spoken back.';
  } else {
    el('hint').textContent = 'This browser has no speech recognition (Chrome/Edge only) — '
      + 'type instead. Replies are still spoken back to you.';
  }

  // One message, once per call — a warning repeated every three seconds is
  // just a different kind of silence.
  function noTranscriptWarning(why) {
    if (!warnedNoTranscript) {
      warnedNoTranscript = true;
      add('err', 'Speech-to-text is not working here — ' + why + '. '
        + 'Speech recognition needs Chrome or Edge with an internet connection; '
        + 'Firefox and plain Chromium have no engine for it. '
        + 'Press "diag" above for the raw event log. '
        + 'Typing below works exactly the same, and replies are still spoken back.');
      diagOpen = true;
      const panel = el('diagpanel');
      if (panel) { panel.hidden = false; renderDiag(); }
    }
    setState('type instead', 'err', 'error');
  }

  // Heard a voice for NO_TRANSCRIPT_AFTER_MS and got no text back. Before
  // giving up, test the one hypothesis this app can test by itself: the level
  // meter holds its own getUserMedia stream for the orb, and some browsers
  // will not hand the same microphone to the recogniser at the same time.
  // Drop the meter and try once more — if a transcript arrives, that was it,
  // and the call keeps working (the orb just stops tracking amplitude while
  // listening). If it fails again, say so with the real error attached.
  function onNoTranscript() {
    listeningSince = 0;
    if (!meterReleased && micReady) {
      meterReleased = true;
      meter.closeMic();
      peakSinceResult = 0;
      add('sys', 'No text came back from speech recognition — retrying without '
        + 'the audio level meter, in case it was holding the microphone.');
      // Restart the recogniser cleanly on the freed device.
      try { recog.abort(); } catch (e) { /* not running */ }
      listeningSince = Date.now();
      setTimeout(() => { if (inCall && wantMic) startMic(); }, 400);
      return;
    }
    wantMic = false;
    noTranscriptWarning('your voice is coming through, but this browser is not '
      + 'turning it into text'
      + (lastSrError ? ' (speech recognition reported "' + lastSrError + '")' : '')
      + (srLog.length ? '' : ' — and it emitted no events at all'));
  }

  function startMic() {
    if (!recog || !inCall || !micReady || turnInFlight) return;
    if (!srT0) srT0 = Date.now();
    try {
      recog.start();
      if (!listeningSince) listeningSince = Date.now();
      setState('listening', 'on', 'listening');
    } catch (e) { /* already running */ }
  }
  function stopMic() {
    wantMic = false;
    listeningSince = 0;
    if (recog) { try { recog.stop(); } catch (e) {} }
  }
  function restartMic() {
    // The turn is over the moment we are willing to listen again — this is
    // the single place that clears it, so every path (spoke, muted, TTS
    // failed) converges here.
    turnInFlight = false;
    if (inCall && wantMic && !turnInFlight) startMic();
    else if (inCall) setState('ready', 'on', 'idle');
    else setState('idle', '', 'idle');
  }

  // ---- the call ---------------------------------------------------------
  function hangUp(reason) {
    inCall = false;
    turnInFlight = false;
    stopMic();
    meter.closeMic();
    micReady = false;
    try { audio.pause(); } catch (e) {}
    if (ws) { try { ws.close(); } catch (e) {} ws = null; }
    callBtn.innerHTML = ICON.phone + 'Call';
    callBtn.className = 'primary';
    textIn.disabled = true; sendBtn.disabled = true;
    setState(reason || 'idle', reason === 'error' ? 'err' : '', reason === 'error' ? 'error' : 'idle');
    orb.setLevel(0);
  }

  async function connect() {
    setState('connecting…', 'busy', 'thinking');

    // Ask for the mic BEFORE the socket, so the permission prompt is clearly
    // tied to pressing Call. A refusal is not fatal — the call continues as
    // text-only rather than dying on a permission the user may not want.
    const micErr = await meter.openMic();
    if (micErr) {
      micReady = false;
      add('err', 'No microphone (' + micErr + ') — you can still type. '
        + 'If you meant to talk, allow the mic for this site and press Call again.');
    } else {
      micReady = true;
    }

    ws = new WebSocket(io.wsUrl('/ws/call'));
    ws.onopen = () => { inCall = true; };

    ws.onmessage = (ev) => {
      let m;
      try { m = JSON.parse(ev.data); } catch (e) { return; }

      if (m.type === 'ready') {
        callBtn.innerHTML = ICON.hangup + 'Hang up';
        callBtn.className = 'danger';
        textIn.disabled = false; sendBtn.disabled = false;
        add('sys', 'Connected to ' + m.agent
          + (m.resumed ? ' — resuming your last conversation.' : ' — new conversation.'));
        wantMic = !!recog && micReady;
        if (wantMic) startMic(); else setState('ready', 'on', 'idle');
        textIn.focus();

      } else if (m.type === 'heartbeat') {
        if (turnInFlight) setState('thinking…', 'busy', 'thinking');

      } else if (m.type === 'text_delta') {
        if (!streaming) streaming = add('agent', '');
        streaming.textContent += m.text;
        logEl.scrollTop = logEl.scrollHeight;
        setState('answering…', 'busy', 'thinking');

      } else if (m.type === 'done') {
        if (!streaming) streaming = add('agent', '');
        // Streamed deltas win when there were any; `text` is the whole reply,
        // which is all we get from a runner that emits no llm_token events.
        if (!streaming.textContent) streaming.textContent = m.text;
        const spoken = streaming.textContent;
        streaming = null;
        say(spoken);

      } else if (m.type === 'cleared') {
        add('sys', 'Conversation reset — the next thing you say starts fresh.');

      } else if (m.type === 'error') {
        streaming = null;
        turnInFlight = false;
        add('err', m.message || 'unknown error');
        setState('error', 'err', 'error');
      }
    };

    ws.onclose = () => { if (inCall) hangUp('disconnected'); };
    ws.onerror = () => setState('connection failed', 'err', 'error');
  }

  function send(text) {
    if (!ws || ws.readyState !== 1) return;
    add('me', text);
    turnInFlight = true;
    listeningSince = 0;
    peakSinceResult = 0;
    // Stop listening while the agent answers, or the spoken reply feeds
    // straight back into the recogniser and the call talks to itself.
    if (recog) { try { recog.stop(); } catch (e) {} }
    setState('thinking…', 'busy', 'thinking');
    ws.send(JSON.stringify({ type: 'message', text }));
  }

  callBtn.onclick = () => { if (inCall) hangUp('idle'); else connect(); };
  sendBtn.onclick = () => {
    const t = textIn.value.trim();
    if (t) { textIn.value = ''; send(t); }
  };
  textIn.onkeydown = (e) => { if (e.key === 'Enter') sendBtn.onclick(); };
  speakBtn.onclick = () => {
    speak = !speak;
    speakBtn.innerHTML = (speak ? ICON.on : ICON.off) + (speak ? 'voice on' : 'voice off');
    speakBtn.className = speak ? 'on' : '';
    if (!speak) { try { audio.pause(); } catch (e) {} restartMic(); }
  };
  // Diagnostics. The failure this exists for is invisible by construction —
  // a recogniser that emits nothing looks exactly like one that is patiently
  // listening. Printing the raw event stream turns "it doesn't work" into a
  // specific, reportable fact.
  function renderDiag() {
    const panel = el('diagpanel');
    if (!panel) return;
    const lines = [
      'speech recognition : ' + (recog ? 'available' : 'ABSENT in this browser'),
      'recognition lang   : ' + (recog ? recog.lang : '-'),
      'mic (getUserMedia) : ' + (micReady ? 'open' + (meterReleased ? ' -> level meter released for retry' : '') : 'not open'),
      'peak since result  : ' + peakSinceResult.toFixed(3) + '  (counts as audible above ' + AUDIBLE + ')',
      'last error         : ' + (lastSrError || 'none'),
      'events             : ' + (srLog.length || 'NONE — the recogniser is emitting nothing at all'),
      '',
    ];
    srLog.slice(-24).forEach((e) => {
      lines.push(String(e.ms).padStart(6) + 'ms  ' + e.ev + (e.extra ? '  ' + e.extra : ''));
    });
    panel.textContent = lines.join('\n');
  }

  el('diag').onclick = () => {
    diagOpen = !diagOpen;
    el('diagpanel').hidden = !diagOpen;
    if (diagOpen) renderDiag();
  };

  clearBtn.onclick = () => {
    if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: 'clear' }));
    else add('sys', 'Start a call first.');
  };

  // Feed the orb. One timer for both directions — whichever source is live
  // is the one connected to the analyser.
  levelTimer = setInterval(() => {
    if (dead) return;
    const lv = inCall ? meter.level() : 0;
    orb.setLevel(lv);
    // Peak since the last transcript — the evidence that separates "you said
    // nothing" from "you spoke and nothing came back".
    if (lv > peakSinceResult) peakSinceResult = lv;
    // Once the meter is released we can no longer hear anything, so the
    // "was there a voice" evidence is gone and time alone has to decide —
    // otherwise the second chance could never time out and the retry would
    // hang forever, which is the very failure this whole path exists to end.
    if (inCall && wantMic && !turnInFlight && listeningSince
        && (meterReleased || peakSinceResult > AUDIBLE)
        && Date.now() - listeningSince > NO_TRANSCRIPT_AFTER_MS) {
      onNoTranscript();
    }
  }, 50);

  // The picker is informational: which agent a call reaches is workspace
  // config (Settings), not a per-visitor choice, so this shows what is set
  // and what else exists without letting one tab re-point everyone's calls.
  io.fetch('/settings')
    .then((r) => r.json())
    .then((s) => {
      if (s.default_voice_lang && !navigator.language) {
        lang = s.default_voice_lang;
        if (recog) recog.lang = lang;
      }
      return io.fetch('/agents-list').then((r) => r.json()).then((a) => {
        (a.agents || []).forEach((row) => {
          const o = document.createElement('option');
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
    .catch((e) => add('err', 'Could not load settings: ' + e.message));

  return {
    destroy() {
      dead = true;
      if (levelTimer) clearInterval(levelTimer);
      try { if (ws) ws.close(); } catch (e) {}
      stopMic();
      try { audio.pause(); } catch (e) {}
      orb.destroy();
      meter.destroy();
      if (host.parentNode) host.parentNode.removeChild(host);
    },
  };
}

/* ========================================================================
 * register(host) — component-mode window body
 * ====================================================================== */

export function register(host) {
  const io = {
    apiUrl: (sub) => host.app.apiUrl(sub),
    wsUrl: (sub) => host.app.wsUrl(sub),
    fetch: (sub, init) => host.app.fetch(sub, init),
  };

  function CallAgentBody() {
    const ref = host.React.useRef(null);
    host.React.useEffect(() => {
      if (!ref.current) return undefined;
      const handle = mountCallUI(ref.current, io);
      return () => handle.destroy();
    }, []);
    // position:relative anchors .cag-root's inset:0; BasicWindow gives the
    // body flex-1 with its own scrolling, so the UI owns this box entirely.
    return host.h('div', {
      ref,
      style: { position: 'relative', width: '100%', height: '100%', minHeight: '360px' },
    });
  }

  host.registerWindow(`${SLUG}.main`, CallAgentBody);
}

export default register;
