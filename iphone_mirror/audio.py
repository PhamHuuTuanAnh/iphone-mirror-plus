"""AirPlay real-time audio (RTP over UDP): AES-CBC decryption, AAC-ELD/ALAC decode, playback."""
import asyncio
import collections
import ctypes
import logging
import sys
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


def _default_output_name():
    """Name of the Windows default playback device as waveOut sees it (None if unknown/not Windows).

    Same query PortAudio's MME backend uses to pick its default device; costs microseconds."""
    if sys.platform != "win32":
        return None
    winmm = ctypes.windll.winmm
    dev_id, status = wintypes.DWORD(0), wintypes.DWORD(0)
    rc = winmm.waveOutMessage(
        wintypes.HANDLE(-1), 0x2015,  # WAVE_MAPPER, DRVM_MAPPER_PREFERRED_GET
        ctypes.cast(ctypes.byref(dev_id), ctypes.c_void_p), ctypes.cast(ctypes.byref(status), ctypes.c_void_p),
    )
    if rc != 0:
        return None
    caps = _WAVEOUTCAPSW()
    if winmm.waveOutGetDevCapsW(ctypes.c_size_t(dev_id.value), ctypes.byref(caps), ctypes.sizeof(caps)) != 0:
        return None
    return caps.szPname


if sys.platform == "win32":
    from ctypes import wintypes

    class _WAVEOUTCAPSW(ctypes.Structure):
        _fields_ = [
            ("wMid", wintypes.WORD), ("wPid", wintypes.WORD), ("vDriverVersion", wintypes.UINT),
            ("szPname", wintypes.WCHAR * 32), ("dwFormats", wintypes.DWORD), ("wChannels", wintypes.WORD),
            ("wReserved1", wintypes.WORD), ("dwSupport", wintypes.DWORD),
        ]

    ctypes.windll.winmm.waveOutMessage.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p]
    ctypes.windll.winmm.waveOutMessage.restype = wintypes.UINT
    ctypes.windll.winmm.waveOutGetDevCapsW.argtypes = [ctypes.c_size_t, ctypes.c_void_p, wintypes.UINT]
    ctypes.windll.winmm.waveOutGetDevCapsW.restype = wintypes.UINT


class Player:
    """Small jitter buffer in front of a sounddevice output stream that follows the Windows default device."""

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
        self._device = None  # name of the Windows default output the stream was opened on
        self._stream_lock = threading.Lock()
        self._closed = threading.Event()
        self._open(reinit=False)
        # PortAudio picks its default device once (at init) and never follows Windows: a Bluetooth
        # headset connected later, or a different default chosen in Windows, kept getting nothing.
        threading.Thread(target=self._follow_default, name="audio-device", daemon=True).start()

    def _open(self, reinit):
        """(Re)open the output stream on the current Windows default device."""
        with self._stream_lock:
            if self._closed.is_set():
                return
            self._close_stream()
            try:
                import sounddevice as sd

                if reinit:  # re-enumerate devices so PortAudio sees the new default
                    sd._terminate()
                    sd._initialize()
                self._device = _default_output_name()
                self._stream = sd.OutputStream(
                    samplerate=self.rate, channels=2, dtype="int16", latency="low", callback=self._callback
                )
                self._stream.start()
                log.info("audio output: %s", self._device or sd.query_devices(kind="output")["name"])
            except Exception as e:
                self._stream = None
                log.error("audio output unavailable: %s", e)

    def _close_stream(self):
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def _follow_default(self):
        while not self._closed.wait(1.0):
            try:
                name = _default_output_name()
            except Exception:
                continue
            if name is None:
                continue
            stream = self._stream
            dead = stream is not None and not stream.active  # died under us (device removed)
            if name != self._device or dead:
                log.info("audio output changed: %s -> %s%s", self._device, name, " (stream stopped)" if dead else "")
                self.flush()
                self._open(reinit=True)

    def close(self):
        self._closed.set()
        with self._stream_lock:
            self._close_stream()

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
