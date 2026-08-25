"""Server-side SIP speech pipeline: PCM -> Whisper -> agent -> Edge TTS -> PCM."""
from __future__ import annotations

import asyncio
import os
import tempfile
import wave
from collections.abc import Callable

from .call_history import CallStore
from .service import CallAgentService
from .service import CallAgentError


class SipSpeechPipeline:
    def __init__(self, settings_provider: Callable, store: CallStore):
        self.settings_provider = settings_provider
        self.store = store
        self._model = None
        self._sessions: dict[str, str | None] = {}

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

    async def handle(self, call_id: str, pcm: bytes) -> bytes:
        settings = self.settings_provider()
        # A PBX/media test remains useful before Agents Platform is configured.
        if not settings.agents_platform_base:
            return b""
        # SIP audio is narrow-band and short, which makes automatic language
        # detection unstable (Portuguese was repeatedly classified as Arabic,
        # Greek and Dutch).  Settings already carry the caller language, so
        # use its ISO-639 prefix as Whisper's explicit language hint.
        language = (settings.default_voice_lang or "").split("-", 1)[0].lower() or None
        if settings.stt_provider == "openai":
            transcript = await self._transcribe_openai(pcm, language, settings)
        elif settings.stt_provider == "faster-whisper":
            transcript = await asyncio.to_thread(self._transcribe_sync, pcm, language)
        else:
            raise CallAgentError(
                f"unsupported STT provider: {settings.stt_provider}")
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
