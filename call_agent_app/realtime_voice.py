"""Full-duplex OpenAI Realtime voice session for an AudioSocket call."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import sys
from array import array
from collections.abc import Awaitable, Callable

import httpx

from .call_history import CallStore
from .service import CallAgentError, CallSettings

log = logging.getLogger("aw_apps.call_agent.realtime_voice")


def pcm8k_to_pcm24k(pcm: bytes) -> bytes:
    source = array("h")
    source.frombytes(pcm[:len(pcm) - len(pcm) % 2])
    if sys.byteorder != "little":
        source.byteswap()
    output = array("h")
    previous = None
    for current in source:
        if previous is None:
            output.extend((current, current, current))
        else:
            output.extend((previous, int((2 * previous + current) / 3),
                           int((previous + 2 * current) / 3)))
        previous = current
    if sys.byteorder != "little":
        output.byteswap()
    return output.tobytes()


def pcm24k_to_pcm8k(pcm: bytes) -> bytes:
    source = array("h")
    source.frombytes(pcm[:len(pcm) - len(pcm) % 2])
    if sys.byteorder != "little":
        source.byteswap()
    # Realtime speech is already band-limited. Pick the final sample from each
    # interpolated triplet; this preserves timing/amplitude and produces the
    # exact PCM8 format AudioSocket expects without spawning ffmpeg.
    output = array("h", source[2::3])
    if sys.byteorder != "little":
        output.byteswap()
    return output.tobytes()


class OpenAIRealtimeVoiceSession:
    """One persistent audio-in/audio-out session per phone call."""

    def __init__(self, call_id: str, settings: CallSettings, store: CallStore,
                 emit: Callable[[bytes], Awaitable[None]]):
        self.call_id = call_id
        self.settings = settings
        self.store = store
        self.emit = emit
        self.ws = None
        self.reader_task: asyncio.Task | None = None
        self._transcripts: asyncio.Queue[str] = asyncio.Queue()
        self._last_transcript = ""
        self._reply_parts: list[str] = []
        self._persist_tasks: set[asyncio.Task] = set()
        self._closed = False

    async def _instructions(self) -> str:
        base = self.settings.prompt_template.replace("${text}", "").strip()
        if not self.settings.agents_platform_base:
            return base
        headers = ({"Authorization": f"Bearer {self.settings.agents_platform_token}"}
                   if self.settings.agents_platform_token else {})
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.get(
                    f"{self.settings.agents_platform_base.rstrip('/')}/api/agents/"
                    f"{self.settings.agent_slug}", headers=headers)
                response.raise_for_status()
                agent = response.json()
        except Exception as exc:
            log.warning("could not load realtime control plane for %s: %s",
                        self.settings.agent_slug, exc)
            return base
        system = str(agent.get("system_prompt") or "").strip()
        capabilities = str(agent.get("capabilities") or "").strip()
        return "\n\n".join(part for part in (
            system, capabilities, base,
            "You are in a live full-duplex telephone call. Respond briefly in "
            "the caller's language. If interrupted, stop immediately and answer "
            "the newest utterance. Never promise to use a tool later; use it now "
            "or clearly say it is unavailable.",
        ) if part)

    async def connect(self) -> None:
        if not self.settings.openai_api_key:
            raise CallAgentError("OpenAI Realtime voice requires `openai_api_key`")
        import websockets
        self.ws = await websockets.connect(
            f"wss://api.openai.com/v1/realtime?model={self.settings.realtime_model}",
            additional_headers={"Authorization": f"Bearer {self.settings.openai_api_key}"},
            open_timeout=15, close_timeout=5, max_size=8 * 1024 * 1024)
        await self.ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": await self._instructions(),
                "output_modalities": ["audio"],
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "transcription": {"model": self.settings.stt_openai_model,
                                          "language": self.settings.default_voice_lang.split("-")[0]},
                        "turn_detection": {
                            "type": "server_vad", "create_response": True,
                            "interrupt_response": True,
                            "silence_duration_ms": int(self.settings.speech_pause_ms),
                        },
                    },
                    "output": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "voice": self.settings.realtime_voice,
                    },
                },
            },
        }))
        self.reader_task = asyncio.create_task(self._read_events())

    async def append_audio(self, pcm8k: bytes) -> None:
        if self.ws is None:
            return
        await self.ws.send(json.dumps({
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(pcm8k_to_pcm24k(pcm8k)).decode("ascii"),
        }))

    async def interrupt(self) -> None:
        if self.ws is None:
            return
        try:
            await self.ws.send(json.dumps({"type": "response.cancel"}))
        except Exception:
            pass

    async def _read_events(self) -> None:
        try:
            async for raw in self.ws:
                event = json.loads(raw)
                kind = event.get("type", "")
                if kind in {"response.output_audio.delta", "response.audio.delta"}:
                    audio = base64.b64decode(event.get("delta") or "")
                    if audio:
                        await self.emit(pcm24k_to_pcm8k(audio))
                elif kind == "conversation.item.input_audio_transcription.completed":
                    self._last_transcript = str(event.get("transcript") or "").strip()
                elif kind in {"response.output_audio_transcript.delta",
                              "response.audio_transcript.delta"}:
                    self._reply_parts.append(str(event.get("delta") or ""))
                elif kind in {"response.output_audio_transcript.done",
                              "response.audio_transcript.done"}:
                    transcript = str(event.get("transcript") or "").strip()
                    if transcript:
                        self._reply_parts = [transcript]
                elif kind == "response.done":
                    reply = "".join(self._reply_parts).strip()
                    self._reply_parts.clear()
                    # The input transcription completion can arrive just
                    # after response.done. Persist outside the reader loop so
                    # that event remains receivable and history keeps the
                    # spoken user turn paired with the answer.
                    async def persist(completed_reply: str):
                        await asyncio.sleep(0.25)
                        transcript = self._last_transcript
                        self._last_transcript = ""
                        if transcript or completed_reply:
                            self.store.append_text(
                                self.call_id, transcript=transcript,
                                agent_text=completed_reply)
                    task = asyncio.create_task(persist(reply))
                    self._persist_tasks.add(task)
                    task.add_done_callback(self._persist_tasks.discard)
                elif kind == "error":
                    detail = event.get("error") or {}
                    log.warning("Realtime call %s error: %s", self.call_id,
                                detail.get("message") or detail)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closed:
                log.warning("Realtime call %s closed: %s", self.call_id, exc)

    async def close(self) -> None:
        self._closed = True
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
        if self.reader_task is not None:
            self.reader_task.cancel()
            await asyncio.gather(self.reader_task, return_exceptions=True)
        if self._persist_tasks:
            await asyncio.gather(*self._persist_tasks, return_exceptions=True)
