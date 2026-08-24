"""Server-side SIP speech pipeline: PCM -> Whisper -> agent -> Edge TTS -> PCM."""
from __future__ import annotations

import asyncio
import os
import tempfile
import wave
from collections.abc import Callable

from .call_history import CallStore
from .service import CallAgentService


class SipSpeechPipeline:
    def __init__(self, settings_provider: Callable, store: CallStore):
        self.settings_provider = settings_provider
        self.store = store
        self._model = None
        self._sessions: dict[str, str | None] = {}

    def _transcribe_sync(self, pcm: bytes) -> str:
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
                item.name, vad_filter=True, beam_size=3)
            return " ".join(segment.text.strip() for segment in segments).strip()

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

    async def handle(self, call_id: str, pcm: bytes) -> bytes:
        settings = self.settings_provider()
        # A PBX/media test remains useful before Agents Platform is configured.
        if not settings.agents_platform_base:
            return b""
        transcript = await asyncio.to_thread(self._transcribe_sync, pcm)
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

    def forget(self, call_id: str) -> None:
        self._sessions.pop(call_id, None)
