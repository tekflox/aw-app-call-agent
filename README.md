# aw-app-call-agent

Talk to your workspace agent out loud.

Open the **Call Agent** window, hit call, and speak. Your voice is transcribed
in the browser, sent to the agent you picked, and the reply is streamed back
token by token and spoken to you in your own language. Every call resumes the
same conversation instead of starting from scratch.

A call is really **a WebSocket carrying text** — speech-to-text happens on the
client and text-to-speech is one HTTP GET. Nothing about the protocol is
audio, which is why the same socket serves the browser panel and the iOS
`StreamingCallStore` client unchanged.

## Where this came from

Ported from `agentic-workspace/src/meta_display/call_agent.py`, which was
itself the standalone `aw-call-agent` FastAPI service folded into the
monolith's meta_display process in 2026-07.

**Carried over as-is** — the Edge-TTS voice map and its iOS `en_US`
underscore quirk, raw-MP3 (never transcoded) TTS output, markdown stripping
before speech, the session-resume reconstruction over the Agents Platform
REST API, the event-poll streaming loop, and **both** heartbeat paths. Those
last two are load-bearing regression fixes, not defensive noise: an agent can
emit zero `llm_token` events for a long stretch (container cold start, session
resume, any tool-call phase) and the dispatch POST itself can queue behind the
executor's per-Target lock. Without heartbeats a healthy turn gets killed by
the client's stall watchdog. `call_agent_app/service.py` cites the monolith
line numbers for each.

**Changed, and why:**

| Monolith | Here |
|---|---|
| Mixed into `MetaDisplayRoutes`; every turn broadcast to the glasses webapp, Watch and history log | No meta_display exists in aw-workspace — the WS client is the only surface. The durable record is Agents Platform's own run/event log. |
| Target auto-provisioned by `run_sync` | `POST /api/agents/<slug>/run` 404s on an unknown target, so `ensure_target()` creates it on first call (409 = someone else won the race). |
| Unauthenticated local instance | agents-platform-multitenant's `require_identity()` rejects anonymous calls — every request carries a bearer token. |
| `AGENT_SLUG` / `EXTERNAL_ID` / the `/aw-apple-watch` prompt header hardcoded for one iOS app | `agent_slug`, `external_id`, `prompt_template` in Settings. |
| Walked *every* run of the Target looking for a session id | Bounded to the newest 25, filtered server-side to `system.init` — a long-lived Target has thousands. |

## Routes

```
GET /api/apps/call-agent/health          liveness + is a backend configured
GET /api/apps/call-agent/settings        effective settings, token masked
GET /api/apps/call-agent/agents-list     agent picker rows, proxied from AP
GET /api/apps/call-agent/tts?text=&lang= raw MP3 (audio/mpeg)
GET /api/apps/call-agent/panel           the browser call UI
GET /api/apps/call-agent/panel/status    read-only diagnostics
WS  /api/apps/call-agent/ws/call         the call
GET /api/apps/call-agent/telephony/status          SIP + Asterisk readiness
GET /api/apps/call-agent/telephony/config-preview  redacted Asterisk config
POST /api/apps/call-agent/telephony/calls          originate a PSTN call
POST /api/apps/call-agent/telephony/calls/hangup   hang up an Asterisk channel
```

The protocol and the failure playbook live in
[`skills/aw-call-agent/SKILL.md`](skills/aw-call-agent/SKILL.md).

## The UI, and why it is not an iframe

The window body is **component mode** — `ui/dist/call-agent.js` is loaded
into the SPA's own document. That is not a style preference, it is what makes
the microphone work at all.

The first version rendered the panel through the declarative `iframe` widget
and the mic never worked. `aw-workspace-ui/src/components/AppWindow.jsx:296`
builds that `<iframe>` with a `sandbox` attribute and **no `allow`**, and the
panel is served from the API host while the SPA runs on the workspace host —
a cross-origin frame with no `allow="microphone"` is denied the mic by
Permissions Policy before any script runs. `SpeechRecognition` fails with
`not-allowed` and the browser never even offers a prompt. Confirmed in the
live DOM 2026-08-15: the attribute came back `null`.

Core gaining an `allow` passthrough on that widget would be worth having for
every app that wants mic, camera or clipboard in a declarative window — but
it is not this app's change to make, and component mode needs nothing from
core.

The bundle is one hand-written ES module with two entry points — `register`
for the SPA, `mountCallUI` for the `/panel` shell — so the window and
standalone mode cannot drift. There is no build step: no JSX, no npm
dependency (React comes from `host.React`, never imported), so a bundler
would only add a way for the shipped file to drift from the source. The
markup shell stays an `HTMLResponse` route because core serves everything
under `/api/apps/<slug>/ui/` that isn't `.js` as `application/octet-stream`,
so an `index.html` there downloads instead of rendering.

The orb is a rebuild, not a port — the standalone `aw-call-agent` repo it
came from no longer exists. It is an indicator rather than decoration: idle
breathes, listening tracks your voice, thinking spins with no level input
(the agent is deliberately quiet during tool calls), speaking tracks the
reply audio. A silent call is never ambiguous.

## The call window

- **Agent picker** — filter-as-you-type over every agent on the platform
  (there are dozens). Picking one sends `set_agent`, which re-points the
  socket *and* its conversation target, for that call only.
- **Pause** — how much silence ends your turn. Chrome's own end-of-speech is
  sub-second and cuts people off mid-thought, so the recogniser runs
  `continuous` and this client decides instead. Default 2s
  (`speech_pause_ms`).
- **Language** — what you speak and what you hear. Comes from
  `default_voice_lang`, not the browser's UI language.
- **diag** — the raw `SpeechRecognition` event stream, the language and where
  it came from, mic state and the last error. Opens itself when
  speech-to-text fails, which is otherwise indistinguishable from silence.

## Configuration

Leave `agents_platform_base` and `agents_platform_token` **blank** and the app
inherits both from the Agents Platform Runners app, which every workspace that
can run an agent already has configured. Fill them in only to point calls at a
different platform. `GET /settings` reports which layer won
(`app-config` → `env` → `inherited:agents-platform-runners` → `unset`), so a
blank one is diagnosable without reading a log.

Pick an agent that answers in short spoken prose — a coding agent will narrate
its tool calls at you.

## SIP telephony (Zadarma + Asterisk)

Telephony is built into this app's control plane but is deliberately disabled
by default. Configure the Zadarma SIP login/password, the Portuguese number in
E.164 form, and a local Asterisk AMI secret in Settings; only then enable
`telephony_enabled`. The call window's **Phone** button remains disabled until
both the settings and a live AMI ping pass.

`GET /telephony/config-preview` renders the exact minimal `pjsip.conf`,
`extensions.conf` and `manager.conf` the deployment needs, with both SIP and
AMI secrets redacted. Incoming calls enter `AudioSocket` on port 9019; outgoing
calls are queued through AMI into the `call-agent-outbound` context.

Version 0.12 runs as a Tier-2 container with Asterisk in the same app image.
The manifest publishes `5060/udp` and `10000-10100/udp`; the workspace runtime
expands those bindings while keeping the HTTP UI/API on its authenticated
reverse proxy. Settings are projected into the container environment and a
save recreates the container, so ramals, generated passwords, LAN address and
the optional Zadarma trunk are deployed by the app rather than copied by hand.

The full setup and call path are documented in
[`docs/ZADARMA_ASTERISK.md`](docs/ZADARMA_ASTERISK.md). Call metadata and WAV
recordings are durable under `.aw-workspace/data/call-agent/`; the Call window
lists them and loads protected audio into an inline player on demand.

No provider is required for a first test. Expand **Call history and
recordings** and click **Run internal audio test**: the app sends a generated
tone through its real loopback AudioSocket server, persists the WAV and loads
it through the same player used by live calls. The normal browser **Call**
button separately tests microphone transcription, agent dispatch and TTS.

## Development

```bash
python3 -m pytest tests/ -q          # routes, WS protocol, ported helpers
python3 tests/validate_manifest.py   # manifest + referenced files + widgets

AW_AGENTS_PLATFORM_BASE=http://127.0.0.1:10014 \
AW_AGENTS_PLATFORM_TOKEN=<jwt> \
python3 -m call_agent_app            # standalone on 127.0.0.1:9412
```

Standalone mounts the same sub-app at the same prefix with no `IdentityGuard`,
and `GET /` redirects to the panel — same UI, same paths, same protocol.
