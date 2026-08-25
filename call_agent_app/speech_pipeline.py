"""Server-side SIP speech pipeline: PCM -> Whisper -> agent -> Edge TTS -> PCM."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import tempfile
import wave
import sys
from array import array
from collections.abc import Callable

from .call_history import CallStore
from .service import CallAgentService
from .service import CallAgentError

log = logging.getLogger("aw_apps.call_agent.speech_pipeline")


class OpenAIRealtimeTranscriber:
    """One live transcription WebSocket per phone call.

    Asterisk supplies 8 kHz PCM; the Realtime API consumes 24 kHz PCM. Audio
    is resampled and sent in 100 ms batches while the caller is speaking, so
    only the final commit remains after local VAD closes the turn.
    """

    def __init__(self, api_key: str, model: str, delay: str,
                 languages: list[str]):
        self.api_key = api_key
        self.model = model
        self.delay = delay
        self.languages = languages
        self.ws = None
        self.reader_task = None
        self.buffer = bytearray()
        self.last_sample: int | None = None
        self.finals: asyncio.Queue = asyncio.Queue()

    async def connect(self):
        import websockets
        self.ws = await websockets.connect(
            "wss://api.openai.com/v1/realtime?intent=transcription",
            additional_headers={"Authorization": f"Bearer {self.api_key}"},
            open_timeout=15, close_timeout=5)
        await self.ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "type": "transcription",
                "audio": {"input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "transcription": {
                        "model": self.model,
                        "languages": self.languages,
                        "delay": self.delay,
                    },
                    "turn_detection": None,
                }},
            },
        }))
        self.reader_task = asyncio.create_task(self._read_events())

    async def _read_events(self):
        try:
            async for raw in self.ws:
                event = json.loads(raw)
                kind = event.get("type")
                if kind == "conversation.item.input_audio_transcription.completed":
                    await self.finals.put(str(event.get("transcript") or "").strip())
                elif kind == "error":
                    detail = event.get("error") or {}
                    await self.finals.put(CallAgentError(
                        f"OpenAI Realtime STT failed: {detail.get('message') or detail}"))
        except Exception as exc:
            await self.finals.put(exc)

    async def append(self, pcm_8k: bytes):
        source = array("h")
        source.frombytes(pcm_8k[:len(pcm_8k) - len(pcm_8k) % 2])
        if sys.byteorder != "little":
            source.byteswap()
        pcm_24k_samples = array("h")
        previous = self.last_sample
        for current in source:
            if previous is None:
                pcm_24k_samples.extend((current, current, current))
            else:
                pcm_24k_samples.extend((
                    previous,
                    int((2 * previous + current) / 3),
                    int((previous + 2 * current) / 3),
                ))
            previous = current
        self.last_sample = previous
        if sys.byteorder != "little":
            pcm_24k_samples.byteswap()
        pcm_24k = pcm_24k_samples.tobytes()
        self.buffer.extend(pcm_24k)
        # 100 ms of 24 kHz mono PCM16.
        while len(self.buffer) >= 4800:
            chunk = bytes(self.buffer[:4800])
            del self.buffer[:4800]
            await self._send_audio(chunk)

    async def _send_audio(self, pcm: bytes):
        await self.ws.send(json.dumps({
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(pcm).decode("ascii"),
        }))

    async def commit(self) -> str:
        if self.buffer:
            await self._send_audio(bytes(self.buffer))
            self.buffer.clear()
        await self.ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        result = await asyncio.wait_for(self.finals.get(), timeout=15)
        if isinstance(result, Exception):
            raise result
        return result

    async def close(self):
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                # Closing an already-closed websockets transport can raise a
                # transport-level RuntimeError. Cleanup must stay idempotent.
                pass
        if self.reader_task is not None:
            self.reader_task.cancel()


class SipSpeechPipeline:
    def __init__(self, settings_provider: Callable, store: CallStore):
        self.settings_provider = settings_provider
        self.store = store
        self._model = None
        self._sessions: dict[str, str | None] = {}
        self._realtime: dict[str, OpenAIRealtimeTranscriber] = {}

    async def start_call(self, call_id: str) -> None:
        settings = self.settings_provider()
        if settings.stt_provider != "openai-realtime":
            return
        if not settings.openai_api_key:
            raise CallAgentError(
                "OpenAI Realtime STT is selected but `openai_api_key` is empty")
        language = (settings.default_voice_lang or "pt").split("-", 1)[0].lower()
        client = OpenAIRealtimeTranscriber(
            settings.openai_api_key, settings.stt_realtime_model,
            settings.stt_realtime_delay, [language])
        try:
            await client.connect()
        except Exception as exc:
            log.warning("Realtime STT unavailable for call %s; the turn will "
                        "fall back to completed-file STT: %s", call_id, exc)
        else:
            self._realtime[call_id] = client

    async def start_utterance(self, call_id: str) -> None:
        # Realtime transcription sockets can be closed by either side after a
        # commit.  A follow-up must never inherit that dead transport.  The
        # first turn normally uses the socket pre-opened by ``start_call``;
        # later turns reconnect here before their first audio frame.
        client = self._realtime.get(call_id)
        if (client is not None and client.ws is not None and
                client.reader_task is not None and not client.reader_task.done()):
            return
        if client is not None:
            await client.close()
            self._realtime.pop(call_id, None)
        settings = self.settings_provider()
        if settings.stt_provider != "openai-realtime" or not settings.openai_api_key:
            return
        language = (settings.default_voice_lang or "pt").split("-", 1)[0].lower()
        replacement = OpenAIRealtimeTranscriber(
            settings.openai_api_key, settings.stt_realtime_model,
            settings.stt_realtime_delay, [language])
        try:
            await replacement.connect()
        except Exception as exc:
            log.warning("Realtime STT reconnect failed for call %s; using fallback: %s",
                        call_id, exc)
        else:
            self._realtime[call_id] = replacement

    async def append_audio(self, call_id: str, pcm: bytes) -> None:
        client = self._realtime.get(call_id)
        if client is not None:
            try:
                await client.append(pcm)
            except Exception as exc:
                # The complete utterance is retained by AudioSocket, so a
                # dead live transport can safely fall back to file STT.
                log.warning("Realtime STT stream closed for call %s; using fallback: %s",
                            call_id, exc)
                self._realtime.pop(call_id, None)
                await client.close()

    async def commit_utterance(self, call_id: str) -> str | None:
        client = self._realtime.get(call_id)
        if client is None:
            return None
        try:
            return await client.commit()
        finally:
            # One socket per utterance is slightly more conservative than
            # reusing it, but makes follow-ups deterministic across server
            # closes. The next socket is opened as soon as speech starts.
            self._realtime.pop(call_id, None)
            await client.close()

    def _transcribe_sync(self, pcm: bytes, language: str | None = None) -> str:
        if self._model is None:
            from faster_whisper import WhisperModel
            self._model = WhisperModel(
                os.environ.get("AW_CALL_STT_MODEL", "base"),
                device="cpu", compute_type="int8")
        with tempfile.NamedTemporaryFile(suffix=".wav") as item:
            with wave.open(item.name, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(8000)
                wav.writeframes(pcm)
            segments, _info = self._model.transcribe(
                item.name, vad_filter=True, beam_size=3, language=language)
            return " ".join(segment.text.strip() for segment in segments).strip()

    @staticmethod
    def _wav_bytes(pcm: bytes) -> bytes:
        import io
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(8000)
            wav.writeframes(pcm)
        return output.getvalue()

    async def _transcribe_openai(self, pcm: bytes, language: str | None,
                                 settings) -> str:
        if not settings.openai_api_key:
            raise CallAgentError(
                "OpenAI STT is selected but `openai_api_key` is empty")
        import httpx
        data = {"model": settings.stt_openai_model}
        if language:
            data["language"] = language
        async with httpx.AsyncClient(timeout=90.0) as client:
            response = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                data=data,
                files={"file": ("call.wav", self._wav_bytes(pcm), "audio/wav")},
            )
        if response.status_code >= 400:
            raise CallAgentError(
                f"OpenAI STT failed ({response.status_code}): "
                f"{response.text[:200]}")
        return str(response.json().get("text") or "").strip()

    async def _to_pcm(self, mp3: bytes) -> bytes:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
            "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1", "-ar", "8000",
            "pipe:1", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate(mp3)
        if process.returncode:
            raise RuntimeError(f"ffmpeg TTS conversion failed: {stderr.decode(errors='replace')[:200]}")
        return stdout

    async def _transcript(self, call_id: str, pcm: bytes,
                          live_transcript: str | None = None) -> tuple[str, object]:
        settings = self.settings_provider()
        # A PBX/media test remains useful before Agents Platform is configured.
        if not settings.agents_platform_base:
            return "", settings
        # SIP audio is narrow-band and short, which makes automatic language
        # detection unstable (Portuguese was repeatedly classified as Arabic,
        # Greek and Dutch).  Settings already carry the caller language, so
        # use its ISO-639 prefix as Whisper's explicit language hint.
        language = (settings.default_voice_lang or "").split("-", 1)[0].lower() or None
        if live_transcript is not None:
            transcript = live_transcript
        elif settings.stt_provider == "openai":
            transcript = await self._transcribe_openai(pcm, language, settings)
        elif settings.stt_provider == "faster-whisper":
            transcript = await asyncio.to_thread(self._transcribe_sync, pcm, language)
        elif settings.stt_provider == "openai-realtime":
            # A failed live session can still recover the turn through the
            # completed-file endpoint without losing what the caller said.
            transcript = await self._transcribe_openai(pcm, language, settings)
        else:
            raise CallAgentError(
                f"unsupported STT provider: {settings.stt_provider}")
        return transcript, settings

    async def handle(self, call_id: str, pcm: bytes) -> bytes:
        transcript, settings = await self._transcript(call_id, pcm)
        if not transcript:
            return b""
        service = CallAgentService(settings)
        session_id = self._sessions.get(call_id)
        if call_id not in self._sessions:
            async with service.client(timeout=15.0) as client:
                await service.ensure_target(client)
                session_id = await service.latest_target_session_id(client)
        run_id = await service.run_agent(settings.build_prompt(transcript), session_id)

        async def noop(*_args):
            return None

        reply, new_session = await service.stream_run(run_id, noop, noop)
        self._sessions[call_id] = new_session or session_id
        self.store.append_text(call_id, transcript=transcript,
                               agent_text=reply, run_id=run_id)
        if not reply:
            return b""
        return await self._to_pcm(await service.tts(reply, settings.default_voice_lang))

    async def handle_stream(self, call_id: str, pcm: bytes, emit,
                            live_transcript: str | None = None) -> None:
        """Speak completed sentences while the agent is still generating."""
        transcript, settings = await self._transcript(
            call_id, pcm, live_transcript)
        if not transcript:
            return
        service = CallAgentService(settings)
        session_id = self._sessions.get(call_id)
        if call_id not in self._sessions:
            async with service.client(timeout=15.0) as client:
                await service.ensure_target(client)
                session_id = await service.latest_target_session_id(client)
        run_id = await service.run_agent(settings.build_prompt(transcript), session_id)

        queue: asyncio.Queue[str | None] = asyncio.Queue()
        pending_text = ""
        spoken_text = ""
        queued_any = False

        async def consume():
            nonlocal spoken_text
            while True:
                sentence = await queue.get()
                if sentence is None:
                    return
                spoken_text += sentence
                audio = await service.tts(sentence, settings.default_voice_lang)
                await emit(await self._to_pcm(audio))

        consumer = asyncio.create_task(consume())

        async def on_delta(delta: str):
            nonlocal pending_text, queued_any
            pending_text += delta
            # Emit natural sentence-sized chunks. The length cap prevents an
            # agent without punctuation from recreating whole-reply latency.
            while True:
                match = __import__("re").search(r"^(.+?[.!?](?:\s+|$))", pending_text, __import__("re").S)
                if match:
                    chunk = match.group(1).strip()
                    pending_text = pending_text[match.end():]
                    await queue.put(chunk)
                    queued_any = True
                elif len(pending_text) >= 160:
                    cut = pending_text.rfind(" ", 0, 160)
                    cut = cut if cut > 40 else 160
                    await queue.put(pending_text[:cut].strip())
                    queued_any = True
                    pending_text = pending_text[cut:].lstrip()
                else:
                    break

        async def noop():
            return None

        reply, new_session = await service.stream_run(run_id, on_delta, noop)
        if pending_text.strip():
            await queue.put(pending_text.strip())
            queued_any = True
        elif not queued_any and not spoken_text and reply.strip():
            await queue.put(reply.strip())
        await queue.put(None)
        await consumer
        self._sessions[call_id] = new_session or session_id
        self.store.append_text(call_id, transcript=transcript,
                               agent_text=reply, run_id=run_id)

    def forget(self, call_id: str) -> None:
        self._sessions.pop(call_id, None)
        client = self._realtime.pop(call_id, None)
        if client is not None:
            asyncio.create_task(client.close())
