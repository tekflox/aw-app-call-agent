"""Tiny SIP/RTP user agent used by the Call Agent's live self-test.

It deliberately implements only the Asterisk path we own: UDP, digest auth,
PCMU, one REGISTER, one INVITE and one BYE.  This is not a general softphone.
"""
from __future__ import annotations

import hashlib
import math
import random
import re
import socket
import struct
import subprocess
import time
import uuid
from dataclasses import dataclass


class SipTestError(RuntimeError):
    pass


@dataclass
class SipTestResult:
    registered: bool
    answered: bool
    sent_audio_bytes: int
    received_audio_bytes: int
    response_peak: int
    elapsed_seconds: float


def _ulaw_encode(sample: int) -> int:
    sample = max(-32635, min(32635, sample))
    sign = 0x80 if sample < 0 else 0
    if sample < 0:
        sample = -sample
    sample += 0x84
    exponent = 7
    mask = 0x4000
    while exponent and not sample & mask:
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa)) & 0xFF


def _ulaw_decode(value: int) -> int:
    value = (~value) & 0xFF
    sample = ((value & 0x0F) << 3) + 0x84
    sample <<= (value >> 4) & 0x07
    sample -= 0x84
    return -sample if value & 0x80 else sample


def pcm16_to_ulaw(pcm: bytes) -> bytes:
    usable = len(pcm) - len(pcm) % 2
    return bytes(_ulaw_encode(sample) for (sample,) in struct.iter_unpack("<h", pcm[:usable]))


def ulaw_peak(payload: bytes) -> int:
    return max((abs(_ulaw_decode(value)) for value in payload), default=0)


def mp3_to_pcm8k(mp3: bytes) -> bytes:
    """Decode TTS output to the exact PCM format used by Asterisk/PCMU."""
    process = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1", "-ar", "8000",
         "pipe:1"],
        input=mp3, capture_output=True, check=False, timeout=30,
    )
    if process.returncode or not process.stdout:
        detail = process.stderr.decode("utf-8", errors="replace")[-300:]
        raise SipTestError(f"TTS audio conversion failed: {detail or 'empty output'}")
    return process.stdout


def _response(raw: bytes) -> tuple[int, dict[str, str], str]:
    text = raw.decode("utf-8", errors="replace")
    head, _, body = text.partition("\r\n\r\n")
    first, *lines = head.split("\r\n")
    match = re.match(r"SIP/2.0\s+(\d+)", first)
    if not match:
        raise SipTestError(f"invalid SIP response: {first[:100]}")
    headers: dict[str, str] = {}
    for line in lines:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    return int(match.group(1)), headers, body


def _digest(challenge: str, username: str, password: str, method: str,
            uri: str) -> str:
    def field(name: str) -> str:
        match = re.search(rf'{name}="?([^",\s]+)', challenge, re.I)
        if not match:
            raise SipTestError(f"digest challenge has no {name}")
        return match.group(1)

    realm, nonce = field("realm"), field("nonce")
    ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
    return (f'Digest username="{username}", realm="{realm}", nonce="{nonce}", '
            f'uri="{uri}", response="{response}", algorithm=MD5')


class SipSoftphoneTester:
    def __init__(self, username: str, password: str, extension: str = "700",
                 host: str = "127.0.0.1", port: int = 5060):
        self.username, self.password, self.extension = username, password, extension
        self.host, self.port = host, port
        # Bind on every interface so this tester also works from a genuinely
        # external host.  Binding to loopback made local PBX tests pass while
        # every packet to a public SIP address failed before leaving macOS.
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect((host, port))
            self.local_address = probe.getsockname()[0]
        finally:
            probe.close()
        self.sip = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sip.bind(("0.0.0.0", 0))
        self.sip.settimeout(8)
        self.rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtp.bind(("0.0.0.0", 0))
        self.rtp.settimeout(0.02)
        self.local_sip_port = self.sip.getsockname()[1]
        self.local_rtp_port = self.rtp.getsockname()[1]
        self.call_id = f"{uuid.uuid4()}@call-agent-test"
        self.from_tag = uuid.uuid4().hex[:10]

    def close(self):
        self.sip.close()
        self.rtp.close()

    def _send(self, method: str, uri: str, cseq: int, *, to: str,
              body: str = "", authorization: str = "", branch: str = ""):
        branch = branch or f"z9hG4bK{uuid.uuid4().hex[:14]}"
        headers = [
            f"{method} {uri} SIP/2.0",
            f"Via: SIP/2.0/UDP {self.local_address}:{self.local_sip_port};branch={branch};rport",
            "Max-Forwards: 70",
            f"From: <sip:{self.username}@{self.host}>;tag={self.from_tag}",
            f"To: {to}",
            f"Call-ID: {self.call_id}",
            f"CSeq: {cseq} {method}",
            f"Contact: <sip:{self.username}@{self.local_address}:{self.local_sip_port}>",
            "User-Agent: aw-call-agent-sip-tester/1",
        ]
        if authorization:
            headers.append(f"Authorization: {authorization}")
        if body:
            headers.extend(["Content-Type: application/sdp", f"Content-Length: {len(body)}"])
        else:
            headers.append("Content-Length: 0")
        packet = "\r\n".join(headers) + "\r\n\r\n" + body
        self.sip.sendto(packet.encode(), (self.host, self.port))

    def _wait_final(self) -> tuple[int, dict[str, str], str]:
        while True:
            code, headers, body = _response(self.sip.recvfrom(65535)[0])
            if code >= 200:
                return code, headers, body

    def _register(self):
        uri = f"sip:{self.host}"
        to = f"<sip:{self.username}@{self.host}>"
        self._send("REGISTER", uri, 1, to=to)
        code, headers, answer = self._wait_final()
        if code != 401:
            raise SipTestError(f"REGISTER expected 401 challenge, got {code}")
        challenge = headers.get("www-authenticate", "")
        self._send("REGISTER", uri, 2, to=to,
                   authorization=_digest(challenge, self.username, self.password,
                                         "REGISTER", uri))
        code, _, _ = self._wait_final()
        if code != 200:
            raise SipTestError(f"REGISTER failed with {code}")

    def _invite(self) -> tuple[tuple[str, int], str]:
        uri = f"sip:{self.extension}@{self.host}"
        to = f"<sip:{self.extension}@{self.host}>"
        sdp = (f"v=0\r\no=- 1 1 IN IP4 {self.local_address}\r\ns=AW SIP test\r\n"
               f"c=IN IP4 {self.local_address}\r\nt=0 0\r\n"
               f"m=audio {self.local_rtp_port} RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n"
               "a=sendrecv\r\n")
        self._send("INVITE", uri, 10, to=to, body=sdp)
        code, headers, _ = self._wait_final()
        if code == 401:
            challenge = headers.get("www-authenticate", "")
            self._send("ACK", uri, 10, to=to)
            self._send("INVITE", uri, 11, to=to, body=sdp,
                       authorization=_digest(challenge, self.username, self.password,
                                             "INVITE", uri))
            code, headers, answer = self._wait_final()
        if code != 200:
            raise SipTestError(f"INVITE failed with {code}")
        to_header = headers.get("to", to)
        self._send("ACK", uri, 11 if "challenge" in locals() else 10, to=to_header)
        port_match = re.search(r"m=audio\s+(\d+)", answer)
        addr_match = re.search(r"c=IN IP4\s+([^\s]+)", answer)
        if not port_match:
            raise SipTestError("answer SDP has no audio port")
        return ((addr_match.group(1) if addr_match else self.host,
                 int(port_match.group(1))), to_header)

    def run(self, pcm: bytes, response_timeout: float = 180) -> SipTestResult:
        started = time.monotonic()
        self._register()
        destination, to_header = self._invite()
        payload = pcm16_to_ulaw(pcm) + (b"\xff" * 8000 * 3)
        seq, timestamp, ssrc = random.randrange(65536), random.randrange(2**32), random.randrange(2**32)
        for offset in range(0, len(payload), 160):
            frame = payload[offset:offset + 160].ljust(160, b"\xff")
            header = struct.pack("!BBHII", 0x80, 0, seq, timestamp, ssrc)
            self.rtp.sendto(header + frame, destination)
            seq, timestamp = (seq + 1) & 0xFFFF, (timestamp + 160) & 0xFFFFFFFF
            time.sleep(0.02)
        received = bytearray()
        peak = 0
        deadline = time.monotonic() + response_timeout
        while time.monotonic() < deadline:
            # Asterisk's AudioSocket application treats a silent RTP source
            # as an inactive call. Keep the caller leg clocking while STT and
            # the agent are thinking, exactly as a real softphone does.
            header = struct.pack("!BBHII", 0x80, 0, seq, timestamp, ssrc)
            self.rtp.sendto(header + (b"\xff" * 160), destination)
            seq, timestamp = (seq + 1) & 0xFFFF, (timestamp + 160) & 0xFFFFFFFF
            try:
                packet, _source = self.rtp.recvfrom(2048)
            except socket.timeout:
                continue
            if len(packet) <= 12:
                continue
            audio = packet[12:]
            received.extend(audio)
            peak = max(peak, ulaw_peak(audio))
            if peak >= 500 and len(received) >= 1600:
                break
        uri = f"sip:{self.extension}@{self.host}"
        try:
            self._send("BYE", uri, 12, to=to_header)
        except OSError:
            pass
        if peak < 500:
            raise SipTestError(
                f"no audible RTP response (received={len(received)} bytes, peak={peak})")
        return SipTestResult(True, True, len(payload), len(received), peak,
                             round(time.monotonic() - started, 2))


def generate_test_pcm(seconds: float = 1.5) -> bytes:
    """Deterministic fallback audio for unit/PBX tests (not used by live TTS)."""
    samples = [int(5000 * math.sin(2 * math.pi * 440 * i / 8000))
               for i in range(int(8000 * seconds))]
    return b"".join(struct.pack("<h", sample) for sample in samples)
