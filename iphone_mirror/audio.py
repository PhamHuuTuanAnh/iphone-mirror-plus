"""AirPlay real-time audio (RTP over UDP): AES-CBC decryption, AAC-ELD/ALAC decode, playback."""
import asyncio
import collections
import logging
import threading

import av
import numpy as np
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from . import net

log = logging.getLogger(__name__)

RATE = 44100

# codec configuration per AirPlay "ct" value
CODECS = {
    2: ("alac", bytes.fromhex("00000024616c616300000000000001600010280a0e0200ff00000000000000000000ac44")),
    4: ("aac", bytes.fromhex("1210")),  # AAC-LC 44.1k stereo
    8: ("aac", bytes.fromhex("f8e85000")),  # AAC-ELD 44.1k stereo, 480 spf
}


class Player:
    """Small jitter buffer in front of a sounddevice output stream."""

    def __init__(self, rate=RATE):
        self.rate = rate
        self.PREBUFFER = int(rate * 0.08)
        self.MAX_BUFFER = int(rate * 0.35)
        self._chunks = collections.deque()
        self._buffered = 0
        self._lock = threading.Lock()
        self._started = False
        self.gain = 1.0
        self.muted = False
        self._stream = None
        try:
            import sounddevice as sd

            self._stream = sd.OutputStream(
                samplerate=rate, channels=2, dtype="int16", latency="low", callback=self._callback
            )
            self._stream.start()
        except Exception as e:
            log.error("audio output unavailable: %s", e)

    def close(self):
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def flush(self):
        with self._lock:
            self._chunks.clear()
            self._buffered = 0
            self._started = False

    def write(self, pcm: np.ndarray):
        """pcm: int16 array shaped (n, 2)."""
        with self._lock:
            self._chunks.append(pcm)
            self._buffered += len(pcm)
            while self._buffered > self.MAX_BUFFER and self._chunks:  # drift: drop oldest
                self._buffered -= len(self._chunks.popleft())
            if not self._started and self._buffered >= self.PREBUFFER:
                self._started = True

    def _callback(self, outdata, frames, time_info, status):
        outdata.fill(0)
        with self._lock:
            if not self._started:
                return
            pos = 0
            while pos < frames and self._chunks:
                chunk = self._chunks[0]
                n = min(frames - pos, len(chunk))
                outdata[pos:pos + n] = chunk[:n]
                if n == len(chunk):
                    self._chunks.popleft()
                else:
                    self._chunks[0] = chunk[n:]
                self._buffered -= n
                pos += n
            if pos < frames:  # underrun: refill before playing again
                self._started = False
        if self.muted:
            outdata.fill(0)
        elif self.gain < 0.999:
            np.multiply(outdata, self.gain, out=outdata, casting="unsafe")


class _Proto(asyncio.DatagramProtocol):
    def __init__(self, handler):
        self.handler = handler

    def datagram_received(self, data, addr):
        try:
            self.handler(data)
        except Exception:
            log.exception("audio packet error")


class AudioStream:
    def __init__(self, aeskey: bytes, aesiv: bytes, ct: int, player: Player):
        self.key, self.iv = aeskey, aesiv
        self.ct = ct
        self.player = player
        self.data_port = self.control_port = 0
        self._transports = []
        self._last_seq = None
        self._decoded = 0
        name, extradata = CODECS.get(ct, CODECS[8])
        self.codec = av.CodecContext.create(name, "r")
        self.codec.extradata = extradata
        self.codec.sample_rate = RATE
        try:
            self.codec.layout = "stereo"
        except Exception:
            pass
        self.resampler = av.AudioResampler(format="s16", layout="stereo", rate=RATE)
        log.info("audio stream ct=%d (%s)", ct, name)

    async def start(self):
        loop = asyncio.get_running_loop()
        t1, _ = await loop.create_datagram_endpoint(lambda: _Proto(self._data), local_addr=(net.BIND_HOST, 0))
        t2, _ = await loop.create_datagram_endpoint(lambda: _Proto(self._control), local_addr=(net.BIND_HOST, 0))
        self._transports = [t1, t2]
        self.data_port = t1.get_extra_info("sockname")[1]
        self.control_port = t2.get_extra_info("sockname")[1]
        return self.data_port, self.control_port

    def stop(self):
        for t in self._transports:
            t.close()
        self._transports = []
        self.player.flush()

    def flush(self):
        self._last_seq = None
        self.player.flush()

    def _control(self, pkt: bytes):
        if len(pkt) > 4 and pkt[1] & 0x7F == 0x56:  # retransmitted packet wrapped in a 4-byte header
            self._data(pkt[4:])

    def _data(self, pkt: bytes):
        if len(pkt) < 12:
            return
        seq = int.from_bytes(pkt[2:4], "big")
        if self._last_seq is not None:
            diff = (seq - self._last_seq) & 0xFFFF
            if diff == 0 or diff > 0x8000:  # duplicate or late
                return
        self._last_seq = seq
        payload = pkt[12:]
        n = len(payload) // 16 * 16
        dec = Cipher(algorithms.AES(self.key), modes.CBC(self.iv)).decryptor()
        clear = dec.update(payload[:n]) + payload[n:]
        try:
            frames = self.codec.decode(av.Packet(clear))
        except av.error.FFmpegError as e:
            log.debug("audio decode error: %s", e)
            return
        for f in frames:
            for r in self.resampler.resample(f):
                pcm = r.to_ndarray().reshape(-1, 2)
                if not self._decoded:
                    log.info("audio decoding OK (%d samples/packet, peak %d)", len(pcm), int(np.abs(pcm).max()))
                self._decoded += 1
                self.player.write(pcm)
