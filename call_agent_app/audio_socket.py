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
import sys
import uuid
from array import array
from collections.abc import Awaitable, Callable

from .call_history import CallStore

log = logging.getLogger("aw_apps.call_agent.audio_socket")

KIND_HANGUP = 0x00
KIND_UUID = 0x01
KIND_DTMF = 0x03
KIND_PCM_8K = 0x10
MAX_FRAME = 65535


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
                 speech_pause_ms: int = 1200,
                 call_finished: Callable[[str], None] | None = None):
        self.store = store
        self.host = host
        self.port = port
        self.server: asyncio.AbstractServer | None = None
        self.active: set[str] = set()
        self.utterance_handler = utterance_handler
        self.speech_pause_ms = speech_pause_ms
        self.call_finished = call_finished

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
        try:
            while True:
                kind, payload = await read_frame(reader)
                if kind == KIND_HANGUP:
                    break
                if kind == KIND_UUID:
                    if len(payload) != 16:
                        raise ValueError("AudioSocket UUID must be 16 bytes")
                    call_id = str(uuid.UUID(bytes=payload))
                    self.store.ensure_call(call_id)
                    self.store.start_recording(call_id)
                    self.active.add(call_id)
                elif kind == KIND_PCM_8K:
                    if not call_id:
                        raise ValueError("AudioSocket audio arrived before UUID")
                    self.store.append_pcm(call_id, payload)
                    if self.utterance_handler is not None:
                        level = pcm_rms(payload)
                        frame_ms = len(payload) / 16.0
                        if level >= 250:
                            speaking = True
                            silence_ms = 0.0
                            utterance.extend(payload)
                        elif speaking:
                            utterance.extend(payload)
                            silence_ms += frame_ms
                            if silence_ms >= self.speech_pause_ms:
                                # AudioSocket closes a call after roughly two
                                # seconds with no traffic in either direction.
                                # STT + an agent run routinely take longer, so
                                # feed quiet PCM while the response is being
                                # prepared to keep the media channel alive.
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
                                loop = asyncio.get_running_loop()
                                deadline = loop.time()
                                for start in range(0, len(response), 320):
                                    chunk = response[start:start + 320]
                                    writer.write(encode_frame(KIND_PCM_8K, chunk))
                                    self.store.append_pcm(call_id, chunk)
                                    # AudioSocket frames are raw media, not a
                                    # downloadable file.  Sending the whole
                                    # reply in one TCP burst makes Asterisk
                                    # emit a matching RTP burst which real
                                    # softphone jitter buffers discard.  Pace
                                    # 320-byte slin/8 kHz frames at 20 ms. Use
                                    # absolute deadlines so scheduler jitter
                                    # does not accumulate across a long reply.
                                    await writer.drain()
                                    deadline += 0.02
                                    await asyncio.sleep(max(0, deadline - loop.time()))
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
            if call_id:
                self.active.discard(call_id)
                self.store.finish(call_id, status=status, error=error)
                if self.call_finished is not None:
                    self.call_finished(call_id)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
