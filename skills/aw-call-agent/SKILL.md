---
name: aw-call-agent
description: The Call Agent app — a voice call into an Agents Platform agent. Covers the WebSocket protocol a client has to speak, the TTS endpoint, how conversation continuity works across calls, and what to check first when a call stalls or answers nothing. Use when wiring a new call client (browser, iOS, phone bridge), when changing which agent calls reach, or when debugging a call that connects but never answers.
---

# Call Agent

A call is **a WebSocket carrying text**. Speech-to-text happens on the client,
the app turns each utterance into an Agents Platform run, streams the reply
back token by token, and hands the text to a TTS endpoint. Nothing about the
protocol is audio — which is why the same socket serves the browser panel, the
iOS `StreamingCallStore`, and anything else you point at it.

Ported from `agentic-workspace/src/meta_display/call_agent.py` (itself the
standalone `aw-call-agent` service, folded into the monolith in 2026-07). The
port dropped meta_display's glasses/Watch broadcast — see `service.py`'s
docstring for what came across and what didn't.

## The wire protocol

`WS /api/apps/call-agent/ws/call` (integrated; auth is the workspace's own
`?token=`-then-cookie `IdentityGuard`, the app implements none of its own).

Client → server:

| Frame | Meaning |
|---|---|
| `{"type":"message","text":"…"}` | one turn — what the caller said |
| `{"type":"clear"}` | forget the resumed session; next turn starts fresh |

Server → client:

| Frame | Meaning |
|---|---|
| `{"type":"ready","agent":…,"target":…,"resumed":bool}` | sent once on connect |
| `{"type":"heartbeat"}` | liveness — see below, it is load-bearing |
| `{"type":"text_delta","text":"…"}` | one `llm_token` delta |
| `{"type":"done","text":"…","run_id":…}` | end of turn, full reply |
| `{"type":"cleared"}` | ack for `clear` |
| `{"type":"error","message":"…"}` | turn failed; socket stays open |

**Do not drop the heartbeats.** They arrive on every poll tick, including
ticks that produced no events, and a client's stall watchdog is expected to
re-arm on them. An agent can spend a long stretch emitting zero `llm_token`
events — container cold start, session resume, any tool-call phase — and a
client that only re-arms on real text will kill a perfectly healthy turn.
Two separate regression fixes in the monolith exist for exactly this; both
came across.

## The other routes

```
GET /api/apps/call-agent/health         liveness + is a backend configured
GET /api/apps/call-agent/settings       effective settings, token masked
GET /api/apps/call-agent/agents-list    agent picker rows, proxied from AP
GET /api/apps/call-agent/tts?text=&lang= raw MP3 (audio/mpeg)
GET /api/apps/call-agent/panel          the browser call UI
GET /api/apps/call-agent/panel/status   read-only diagnostics for Settings
```

`/tts` returns **raw MP3 on purpose**. Every other TTS path in AW transcodes
to OGG/Opus for Telegram; the call clients decode MP3 directly and changing
the format breaks them. Markdown is stripped before synthesis — an agent that
writes `**done**` must not have "asterisk asterisk done" read out.

## Conversation continuity

Every call runs against the Target `<agent_slug>-<external_id>` (default
`telegram-sonnet-aw-call-shared`), created on first use. On connect the app
walks that Target's recent runs newest-first looking for a persisted
`system.init.session_id`, and resumes it — so a call picks up where the last
one left off instead of starting blank. `clear` drops the resume for the rest
of that socket.

Change `external_id` in Settings to split conversations (per-device, per
person); change `agent_slug` to call someone else entirely.

## When a call misbehaves

1. **Open the Settings window first.** Its status panel answers the only
   question that matters — is there a base URL, is there a token, where did
   they come from, and does listing agents actually work. Blank credentials
   are the normal cause and the app says so on the socket too.
2. **Credentials are inherited by default.** With both fields blank, the app
   reads the Agents Platform Runners app's own config. `credentials_source`
   in `/settings` tells you which layer won: `app-config`, `env`,
   `inherited:agents-platform-runners`, or `unset`.
3. **"Connected but never answers"** is usually the agent, not the call. The
   run exists in Agents Platform — check it there by `run_id` from the `done`
   frame, or watch the Target's runs. A run that finishes with `tokens_in: 0`
   is a hard failure wearing a success status.
4. **A silent reply with no deltas** means the agent's runner emits no
   `llm_token` events. The `done` frame's `text` still carries the whole
   reply; that path is deliberate.
5. **Microphone vs transcription are two different failures.** They look
   the same to a user and have nothing to do with each other:
   - *Capture* is `getUserMedia`. `NotAllowedError`/`not-allowed` means a
     permission or policy block; `NotFoundError` means no audio device.
     Verified working 2026-08-15 against a fake audio device fed real
     speech — live track, and the orb visibly tracking the waveform.
   - *Transcription* is `SpeechRecognition`, which hands audio to a cloud
     service (Chrome/Edge, online). A browser without that backend — plain
     Chromium, Firefox, offline/enterprise setups — does something worse
     than fail: `start()` returns without throwing and then **no event ever
     fires**. Not `start`, not `error`, not `end`. Measured on this
     workspace's own Chromium: 30 seconds, zero events, while the mic track
     was live the whole time.
     That is why the app's detector is a **clock, not an event counter** —
     after 12 seconds of audible input with no transcript it says so and
     falls back to typing. If you add a check here, never hang it off a
     `SpeechRecognition` event: in the failing case there are none.
   The text box is a full substitute for either and exercises the identical
   path from the socket onwards.
6. **Check the language before believing anything else.** Press **diag** in
   the call window: it prints the recogniser's language, where that value
   came from, and the raw `SpeechRecognition` event stream with timings.
   A recogniser set to `en-US` while the caller speaks Portuguese returns
   nothing usable — mic prompt appears, level meter sees the voice, no
   transcript ever arrives. It is the single most convincing impostor for a
   broken microphone. The call's language comes from `default_voice_lang` in
   Settings (NOT the browser's UI language, which is a guess about a
   different question) and the picker in the bar overrides it live.

## Picking an agent

Pick one that answers in short spoken prose. A coding agent will narrate its
tool calls at you and the call becomes unlistenable. If the agent expects a
skill slash-command, put it on the first line of `prompt_template` — the
template's `${text}` is replaced with what the caller said, and a template
missing the placeholder gets the caller's words appended rather than dropped.
