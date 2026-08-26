"""Asterisk AudioSocket receiver and recorder.

AudioSocket is intentionally tiny: a one-byte type, a two-byte big-endian
payload length, then the payload. Asterisk sends the call UUID first and
16-bit 8 kHz mono PCM as type 0x10. The bridge records every PCM frame as it
arrives; later STT/agent/TTS stages can consume the same session without
changing persistence or the HTTP history surface.
"""

from __future__ import annotations

import asyncio
import logging
import math
import inspect
import sys
import time
import uuid
from array import array
from collections.abc import Awaitable, Callable

from .call_history import CallStore

log = logging.getLogger("aw_apps.call_agent.audio_socket")
log.setLevel(logging.INFO)
latency_log = logging.getLogger("uvicorn.error")

KIND_HANGUP = 0x00
KIND_UUID = 0x01
KIND_DTMF = 0x03
KIND_PCM_8K = 0x10
MAX_FRAME = 65535
LOCAL_BARGE_IN_RMS = 1000
LOCAL_BARGE_IN_RESET_RMS = 500
LOCAL_BARGE_IN_CONFIRM_MS = 60.0


def pcm_rms(payload: bytes) -> int:
    if len(payload) < 2:
        return 0
    samples = array("h")
    samples.frombytes(payload[:len(payload) - (len(payload) % 2)])
    if sys.byteorder != "little":
        samples.byteswap()
    return int(math.sqrt(sum(value * value for value in samples) / len(samples)))


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    header = await reader.readexactly(3)
    kind = header[0]
    length = int.from_bytes(header[1:3], "big")
    if length > MAX_FRAME:
        raise ValueError("AudioSocket frame too large")
    return kind, await reader.readexactly(length)


def encode_frame(kind: int, payload: bytes = b"") -> bytes:
    if not 0 <= kind <= 255 or len(payload) > MAX_FRAME:
        raise ValueError("invalid AudioSocket frame")
    return bytes((kind,)) + len(payload).to_bytes(2, "big") + payload


class AudioSocketBridge:
    def __init__(self, store: CallStore, host: str = "127.0.0.1", port: int = 9019,
                 utterance_handler: Callable[[str, bytes], Awaitable[bytes]] | None = None,
                 utterance_streamer=None,
                 audio_observer=None,
                 duplex_session_factory=None,
                 speech_pause_ms: int = 1200,
                 call_finished: Callable[[str], None] | None = None):
        self.store = store
        self.host = host
        self.port = port
        self.server: asyncio.AbstractServer | None = None
        self.active: set[str] = set()
        self.utterance_handler = utterance_handler
        self.utterance_streamer = utterance_streamer
        self.audio_observer = audio_observer
        self.duplex_session_factory = duplex_session_factory
        self.speech_pause_ms = speech_pause_ms
        self.call_finished = call_finished
        # Durable-enough, process-local evidence for the live SIP self-test.
        # A turn is counted only after synthesized PCM has been fully written
        # to Asterisk's AudioSocket connection.
        self._response_audio: dict[str, dict[str, int]] = {}

    def response_audio_stats(self, call_id: str) -> dict[str, int]:
        return dict(self._response_audio.get(call_id, {
            "turns": 0, "bytes": 0, "peak": 0,
        }))

    async def start(self):
        if self.server is not None:
            return
        self.server = await asyncio.start_server(self.handle, self.host, self.port)
        # Port 0 is useful in tests; record the selected port.
        self.port = self.server.sockets[0].getsockname()[1]
        log.info("AudioSocket bridge listening on %s:%s", self.host, self.port)

    async def stop(self):
        if self.server is None:
            return
        self.server.close()
        await self.server.wait_closed()
        self.server = None

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        call_id = ""
        status = "completed"
        error = ""
        utterance = bytearray()
        speaking = False
        silence_ms = 0.0
        duplex_session = None
        barge_in_voice_ms = 0.0
        barge_in_latched = False
        output_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        playback_task = None
        output_started = False
        connected_at = time.monotonic()
        first_media_at = 0.0
        last_media_at = 0.0
        media_frames = 0
        late_frames = 0
        max_media_gap_ms = 0.0
        first_emit_at = 0.0
        first_playback_at = 0.0

        async def duplex_emit(response: bytes):
            nonlocal output_started, first_emit_at
            if not response:
                return
            if not output_started and call_id:
                self._response_audio[call_id]["turns"] += 1
                output_started = True
            if not first_emit_at:
                first_emit_at = time.monotonic()
                latency_log.info("call_latency call=%s event=bridge_first_audio session_ms=%.1f",
                         call_id, (first_emit_at - connected_at) * 1000)
            await output_queue.put(response)

        async def playback():
            nonlocal first_playback_at
            deadline = asyncio.get_running_loop().time()
            while True:
                response = await output_queue.get()
                if response is None:
                    return
                for start in range(0, len(response), 320):
                    chunk = response[start:start + 320]
                    writer.write(encode_frame(KIND_PCM_8K, chunk))
                    if not first_playback_at:
                        first_playback_at = time.monotonic()
                        latency_log.info(
                            "call_latency call=%s event=first_playback bridge_queue_ms=%.1f session_ms=%.1f",
                            call_id, (first_playback_at - first_emit_at) * 1000,
                            (first_playback_at - connected_at) * 1000)
                    self.store.append_pcm(call_id, chunk)
                    await writer.drain()
                    stats = self._response_audio[call_id]
                    stats["bytes"] += len(chunk)
                    if chunk:
                        samples = array("h")
                        samples.frombytes(chunk[:len(chunk) - len(chunk) % 2])
                        if sys.byteorder != "little":
                            samples.byteswap()
                        stats["peak"] = max(stats["peak"], max(
                            (abs(value) for value in samples), default=0))
                    deadline = max(deadline + 0.02,
                                   asyncio.get_running_loop().time())
                    await asyncio.sleep(max(0, deadline - asyncio.get_running_loop().time()))
        try:
            while True:
                kind, payload = await read_frame(reader)
                if kind == KIND_HANGUP:
                    break
                if kind == KIND_UUID:
                    if len(payload) != 16:
                        raise ValueError("AudioSocket UUID must be 16 bytes")
                    call_id = str(uuid.UUID(bytes=payload))
                    latency_log.info("call_latency call=%s event=audiosocket_connected", call_id)
                    self.store.ensure_call(call_id)
                    self.store.start_recording(call_id)
                    self.active.add(call_id)
                    self._response_audio[call_id] = {"turns": 0, "bytes": 0, "peak": 0}
                    if len(self._response_audio) > 100:
                        oldest = next(iter(self._response_audio))
                        if oldest != call_id:
                            self._response_audio.pop(oldest, None)
                    if self.audio_observer is not None:
                        await self.audio_observer.start_call(call_id)
                    if self.duplex_session_factory is not None:
                        duplex_session = self.duplex_session_factory(call_id, duplex_emit)
                        await duplex_session.connect()
                        playback_task = asyncio.create_task(playback())
                elif kind == KIND_PCM_8K:
                    if not call_id:
                        raise ValueError("AudioSocket audio arrived before UUID")
                    self.store.append_pcm(call_id, payload)
                    now = time.monotonic()
                    media_frames += 1
                    if not first_media_at:
                        first_media_at = now
                        latency_log.info(
                            "call_latency call=%s event=first_input_audio after_connect_ms=%.1f",
                            call_id, (now - connected_at) * 1000)
                    if last_media_at:
                        gap_ms = (now - last_media_at) * 1000
                        max_media_gap_ms = max(max_media_gap_ms, gap_ms)
                        if gap_ms > 40:
                            late_frames += 1
                    last_media_at = now
                    if duplex_session is not None:
                        level = pcm_rms(payload)
                        frame_ms = len(payload) / 16.0
                        if level >= LOCAL_BARGE_IN_RMS:
                            barge_in_voice_ms += frame_ms
                        elif level < LOCAL_BARGE_IN_RESET_RMS:
                            barge_in_voice_ms = 0.0
                            barge_in_latched = False
                        response_active = bool(getattr(
                            duplex_session, "response_active", False))
                        if (output_started and response_active and
                                not barge_in_latched and
                                barge_in_voice_ms >= LOCAL_BARGE_IN_CONFIRM_MS):
                            # Require sustained, clearly audible input before
                            # treating it as barge-in. A single RTP spike or
                            # speaker echo must not cancel/clear the response.
                            barge_in_latched = True
                            output_started = False
                            while not output_queue.empty():
                                try:
                                    output_queue.get_nowait()
                                except asyncio.QueueEmpty:
                                    break
                            latency_log.info(
                                "call_latency call=%s event=local_barge_in level=%s response_active=%s",
                                call_id, level, response_active)
                            await duplex_session.interrupt()
                        await duplex_session.append_audio(payload)
                        continue
                    if self.utterance_handler is not None:
                        level = pcm_rms(payload)
                        frame_ms = len(payload) / 16.0
                        if level >= 250:
                            if not speaking and self.audio_observer is not None:
                                await self.audio_observer.start_utterance(call_id)
                            speaking = True
                            silence_ms = 0.0
                            utterance.extend(payload)
                            if self.audio_observer is not None:
                                await self.audio_observer.append_audio(call_id, payload)
                        elif speaking:
                            utterance.extend(payload)
                            if self.audio_observer is not None:
                                await self.audio_observer.append_audio(call_id, payload)
                            silence_ms += frame_ms
                            if silence_ms >= self.speech_pause_ms:
                                # AudioSocket closes a call after roughly two
                                # seconds with no traffic in either direction.
                                # STT + an agent run routinely take longer, so
                                # feed quiet PCM while the response is being
                                # prepared to keep the media channel alive.
                                live_transcript = None
                                if self.audio_observer is not None:
                                    try:
                                        live_transcript = await self.audio_observer.commit_utterance(call_id)
                                    except Exception as exc:
                                        log.warning("live transcript failed for %s; using fallback: %s",
                                                    call_id, exc)

                                loop = asyncio.get_running_loop()
                                deadline = None
                                emitted_bytes = 0
                                emitted_peak = 0

                                async def emit(response: bytes):
                                    nonlocal deadline, emitted_bytes, emitted_peak
                                    if deadline is None or deadline < loop.time() - 0.1:
                                        deadline = loop.time()
                                    for start in range(0, len(response), 320):
                                        chunk = response[start:start + 320]
                                        writer.write(encode_frame(KIND_PCM_8K, chunk))
                                        self.store.append_pcm(call_id, chunk)
                                        await writer.drain()
                                        emitted_bytes += len(chunk)
                                        if chunk:
                                            samples = array("h")
                                            samples.frombytes(chunk[:len(chunk) - len(chunk) % 2])
                                            if sys.byteorder != "little":
                                                samples.byteswap()
                                            emitted_peak = max(
                                                emitted_peak,
                                                max((abs(value) for value in samples), default=0),
                                            )
                                        deadline += 0.02
                                        await asyncio.sleep(max(0, deadline - loop.time()))

                                if self.utterance_streamer is not None:
                                    pending = asyncio.create_task(self.utterance_streamer(
                                        call_id, bytes(utterance), emit, live_transcript))
                                else:
                                    pending = asyncio.create_task(
                                        self.utterance_handler(call_id, bytes(utterance)))
                                while not pending.done():
                                    try:
                                        await asyncio.wait_for(
                                            asyncio.shield(pending), timeout=0.5)
                                    except asyncio.TimeoutError:
                                        # Keep NAT and the media channel alive,
                                        # and give a quiet audible cue so a
                                        # human caller knows the agent is still
                                        # processing instead of hanging up on
                                        # an apparently dead line.
                                        tick = int(asyncio.get_running_loop().time() * 2)
                                        if tick % 4 == 0:
                                            cue = array("h", (
                                                int(900 * math.sin(2 * math.pi * 660 * i / 8000))
                                                for i in range(160)))
                                            if sys.byteorder != "little":
                                                cue.byteswap()
                                            keepalive = cue.tobytes()
                                        else:
                                            keepalive = b"\x00\x00" * 160
                                        writer.write(encode_frame(KIND_PCM_8K, keepalive))
                                        await writer.drain()
                                response = pending.result()
                                if response:
                                    await emit(response)
                                if emitted_bytes:
                                    stats = self._response_audio[call_id]
                                    stats["turns"] += 1
                                    stats["bytes"] += emitted_bytes
                                    stats["peak"] = max(stats["peak"], emitted_peak)
                                utterance.clear()
                                speaking = False
                                silence_ms = 0.0
                elif kind == KIND_DTMF:
                    # DTMF is retained by Asterisk/AMI; it is not audio and
                    # must never be written into the WAV stream.
                    continue
        except asyncio.IncompleteReadError:
            pass
        except Exception as exc:
            status, error = "failed", str(exc)
            log.warning("AudioSocket call %s failed: %s", call_id or "unknown", exc)
        finally:
            if duplex_session is not None:
                await duplex_session.close()
            if playback_task is not None:
                await output_queue.put(None)
                playback_task.cancel()
                await asyncio.gather(playback_task, return_exceptions=True)
            if call_id:
                latency_log.info(
                    "call_latency call=%s event=call_summary duration_ms=%.1f media_frames=%s late_frames=%s max_media_gap_ms=%.1f",
                    call_id, (time.monotonic() - connected_at) * 1000,
                    media_frames, late_frames, max_media_gap_ms)
                self.active.discard(call_id)
                self.store.finish(call_id, status=status, error=error)
                if self.call_finished is not None:
                    result = self.call_finished(call_id)
                    if inspect.isawaitable(result):
                        await result
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
