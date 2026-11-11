"""Video and sound from the CoreDevice server, in the shape PhoneWindow expects.

CoreDeviceReceiver plays the role of airplay.Receiver for the window: it owns the
server child, decodes /stream.bin (length-prefixed HEVC access units) with FFmpeg,
scales each frame to the window size and hands BGRA arrays to on_frame, and plays
/audio.bin (s16le 48 kHz stereo PCM).

Two device quirks are handled here, both measured on an iPhone 14 Pro Max:
  * padding: the 1290x2796 screen arrives in a 1296x2816 picture whose extra right
    columns and bottom rows are black. They are measured and cropped away.
  * resolution collapse: under fast motion the encoder draws a smaller picture into
    the top-left corner and fills the rest with flat gray. That corner is stretched
    back to the full size, so a fast swipe looks softer instead of shrinking.
"""
import asyncio
import base64
import json
import logging
import socket
import threading
import time
import urllib.error

import av
import numpy as np

from ..audio import Player
from .server import ServerProcess

log = logging.getLogger(__name__)

AUDIO_RATE = 48000
PAD_DARK_Y = 24  # luma of the black padding (full-range video: 0..~20)
GRAY_Y = 128  # luma of the gray margin of a collapsed frame
MEASURE_EVERY_S = 1.0
DARK_LUMA = 4  # mean luma of a frame from a switched-off screen is 0
STILL_TIMEOUT_S = 20  # no video this long: check the connection (a still screen sends nothing)
STATS_EVERY_S = 30.0  # how often the video timing line goes to the log
NOT_FOUND = (
    "Không thấy iPhone. Cắm cáp, hoặc để iPhone mở khóa và cùng mạng Wi-Fi với máy tính "
    "(cần bật kết nối Wi-Fi một lần khi cắm cáp)."
)


def connection_type():
    """(link, udid) of the iPhone right now: link is "USB" or "Wi-Fi". USB wins when both
    are there, like pymobiledevice3's own choice. With no device in usbmuxd the server found
    the iPhone over Wi-Fi by itself (see server._patch_wifi_tunnel)."""
    try:
        from pymobiledevice3 import usbmux

        devices = asyncio.run(usbmux.list_devices())
    except Exception as e:
        log.debug("usbmux list failed: %s", e)
        devices = []
    for kind, label in (("USB", "USB"), ("Network", "Wi-Fi")):
        for d in devices:
            if getattr(d, "connection_type", "") == kind:
                return label, getattr(d, "serial", None)
    return "Wi-Fi", None


def _content_extent(y: np.ndarray, axis: int):
    """Last index (exclusive) along axis that still holds picture, for the right edge
    (axis=1) or bottom edge (axis=0) of a luma plane; plus whether the margin was gray.
    Returns (extent, kind) with kind in "pad", "gray", "none"."""
    n = y.shape[axis]
    lines = y[:, ::-1] if axis == 1 else y[::-1, :]
    # sample every 8th pixel along each line, look at the outer 64 lines at most
    look = min(64 if axis == 1 else 64, n // 4)
    sample = lines[:, :look][::8, :].T if axis == 1 else lines[:look, :][:, ::8]
    # sample: (look, k) -- one row per line, starting at the edge
    mean = sample.mean(axis=1)
    gray_frac = (np.abs(sample.astype(np.int16) - GRAY_Y) < 8).mean(axis=1)
    if gray_frac[0] >= 0.6:
        kind, test = "gray", gray_frac >= 0.6
    elif mean[0] <= PAD_DARK_Y:
        kind, test = "pad", mean <= PAD_DARK_Y
    else:
        return n, "none"
    k = 0
    while k < len(test) and test[k]:
        k += 1
    if k >= len(test) and kind == "gray":
        # a collapse margin can be far wider than 64 lines: walk the full line set coarsely
        full = lines[::8, :].T if axis == 1 else lines[:, ::8]
        step_mean = (np.abs(full.astype(np.int16) - GRAY_Y) < 8).mean(axis=1)
        k = 0
        while k < len(step_mean) and step_mean[k] >= 0.6:
            k += 1
    return max(1, n - k - 1), kind


class _Frame:
    """Full-resolution, cropped frame for screenshots (PhoneWindow.screenshot)."""

    def __init__(self, frame, w, h):
        self.frame, self.w, self.h = frame, w, h

    def to_ndarray(self, format="bgra"):
        arr = self.frame.to_ndarray(format=format)
        return np.ascontiguousarray(arr[: self.h, : self.w])


class _Decoder:
    """What PhoneWindow touches on rx.decoder: target size and the last frame."""

    def __init__(self):
        self.target = None  # (w, h) device pixels of the phone area
        self.last_frame = None


class CoreDeviceReceiver:
    def __init__(self, settings, on_frame, on_event, on_ready=None):
        self.settings = settings
        self.on_frame = on_frame
        self.on_event = on_event
        self.on_ready = on_ready  # called with the ServerProcess once the stream is up
        self.decoder = _Decoder()
        self.server = None
        self.muted = False
        self.player = None
        self._stop = threading.Event()
        self._threads = []
        self._content = None  # (w, h) of the picture without padding, per coded size
        self._content_key = None
        self._measured = 0.0
        self.content_size = None
        self._dark = False
        self._dark_since = 0.0
        self.screen_off = False  # the iPhone screen is off (see _track_dark)
        self.rotation = 0  # degrees the displayed view is turned clockwise from the content (0/90/180/270)

    # ---------------------------------------------------------------- PhoneWindow API
    def emit(self, event, **info):
        try:
            self.on_event(event, info)
        except Exception:
            log.exception("event handler failed")

    def set_muted(self, muted):
        self.muted = muted
        if self.player is not None:
            self.player.muted = muted

    def rename(self, name):
        pass  # the iPhone does not see a name for this connection

    # ---------------------------------------------------------------- lifecycle
    def start(self):
        self._stop.clear()
        t = threading.Thread(target=self._run, name="coredevice", daemon=True)
        t.start()
        self._threads = [t]

    def stop(self):
        self._stop.set()
        if self.server is not None:
            self.server.stop()
        if self.player is not None:
            self.player.close()
            self.player = None

    def restart(self):
        """Restart the server (used after changing settings such as LTRP)."""
        self.stop()
        for t in self._threads:
            t.join(timeout=3)
        self.start()

    def pause(self):
        """End the mirroring session. iOS blocks the camera and microphone while its screen
        is being captured (Xcode's mirroring too), so this frees them."""
        self.stop()
        self.emit("user_paused")

    def resume(self):
        """Start mirroring again after pause(), without blocking the GUI thread."""
        old = list(self._threads)

        def go():
            for t in old:
                t.join(timeout=5)
            self.start()

        threading.Thread(target=go, name="coredevice-resume", daemon=True).start()

    def _run(self):
        """Sessions until stop(): start the server (it picks the iPhone over USB if the cable
        is in, else over Wi-Fi), stream, and when the connection breaks (cable pulled, Wi-Fi
        lost, phone restarted) throw the server away and start a fresh one. A broken tunnel
        never comes back by itself, so retrying on the old server would loop forever."""
        first = True
        failures = 0
        while not self._stop.is_set():
            streamed = self._session(first)
            first = False
            if self._stop.is_set():
                break
            failures = 0 if streamed else failures + 1
            delay = min(2 + 2 * failures, 10)
            if self._stop.wait(delay):
                break

    def _session(self, first):
        """One server lifetime. Returns True if any video arrived."""
        self._dark = False
        self.screen_off = False
        self.emit("starting_server" if first else "searching", message="")
        self.server = ServerProcess(
            ltrp=bool(self.settings.get("ltrp", True)), audio=True, udid=self.settings.get("udid") or None,
            motion_idr=bool(self.settings.get("motion_idr", False)),
        )
        try:
            self.server.start()
        except OSError as e:
            self.emit("error", message=f"Không chạy được máy chủ CoreDevice: {e}")
            return False
        why = self.server.wait_ready(timeout=60, cancel=self._stop)
        if why is not None:
            if why != "cancelled":
                tail = self.server.log_tail()
                log.warning("CoreDevice server not ready: %s\n%s", why, tail[-1500:])
                self.emit("searching", message=explain(tail) or NOT_FOUND)
            self.server.stop()
            return False
        link, udid = connection_type()
        log.info("iPhone connected over %s (%s)", link, udid or "udid ?")
        if udid and udid != self.settings.get("udid"):
            self.settings["udid"] = udid  # remembered so Wi-Fi only ever looks for this iPhone
            self.settings.save()
        if self.on_ready:
            self.on_ready(self.server)
        self.emit("connecting", name=f"iPhone · {link}" if link else "iPhone", ip="127.0.0.1", link=link)
        session = threading.Event()
        audio = threading.Thread(target=self._audio_loop, args=(session,), name="coredevice-audio", daemon=True)
        audio.start()
        self._frames_in_session = 0
        try:
            self._video_loop()
        finally:
            session.set()
            if self.server is not None:
                self.server.stop()
            player = self.player
            if player is not None:
                player.flush()
            if not self._stop.is_set():
                self.emit("lost")
                self.emit("mirror_stopped")
        audio.join(timeout=3)
        return self._frames_in_session > 0

    # ---------------------------------------------------------------- video
    def _video_loop(self):
        """Stream until the connection is gone: the server exited, or the stream failed
        three times in a row (a dead tunnel answers every request with an error)."""
        errors = 0
        while not self._stop.is_set():
            try:
                self._stream_once()
                errors = 0
            except Exception as e:  # connection drop, decoder failure...
                if self._stop.is_set():
                    return
                errors += 1
                log.warning("video stream (%d): %s", errors, e)
            if not self.server.alive() or errors >= 3:
                return
            if self._stop.wait(0.5 * errors):
                return

    def _stats_add(self, name, seconds):
        """Per-stage timing, logged every STATS_EVERY_S: frames decoded/shown and their cost."""
        st = getattr(self, "_stats", None)
        now = time.monotonic()
        if st is None:
            st = self._stats = {"t": now}
        n, total, worst = st.get(name, (0, 0.0, 0.0))
        st[name] = (n + 1, total + seconds, max(worst, seconds))
        span = now - st["t"]
        if span >= STATS_EVERY_S:
            parts = []
            for key in ("decode", "deliver"):
                n, total, worst = st.get(key, (0, 0.0, 0.0))
                if n:
                    parts.append(f"{key} {n / span:.1f}/s avg {total / n * 1000:.1f} ms max {worst * 1000:.1f} ms")
            log.info("video: %s", "; ".join(parts))
            self._stats = {"t": now}

    def _new_codec(self):
        with self.server.request("/codec", timeout=10) as r:
            info = json.loads(r.read())
        ctx = av.CodecContext.create("hevc", "r")
        ctx.extradata = base64.b64decode(info["description"])
        try:
            ctx.thread_type = "SLICE"
        except Exception:
            pass
        ctx.options = {"flags": "low_delay"}
        return ctx

    def _healthy(self):
        """The server still reaches the device (a still screen sends no video for a while)."""
        try:
            with self.server.request("/codec", timeout=6) as r:
                return r.status == 200
        except Exception:
            return False

    def _stream_once(self):
        ctx = self._new_codec()
        resp = self.server.request("/stream.bin", timeout=STILL_TIMEOUT_S)
        buf = bytearray()
        got_key = False
        newest = None  # decoded frame not shown yet
        try:
            while not self._stop.is_set():
                try:
                    chunk = resp.read1(1 << 16)
                except (TimeoutError, socket.timeout):
                    if self._healthy():
                        return  # just a still screen: reconnect the stream, not an error
                    raise ConnectionError("no video and the server lost the iPhone")
                if not chunk:
                    raise ConnectionError("stream ended")
                buf += chunk
                while len(buf) >= 4:
                    n = int.from_bytes(buf[:4], "big")
                    if len(buf) < 4 + n:
                        break
                    kind, data = buf[4], bytes(buf[5:4 + n])
                    del buf[:4 + n]
                    if kind == 2:  # keyframe after an upstream drop: fresh decoder state
                        ctx = self._new_codec()
                    if kind in (0, 2):
                        got_key = True
                    if not got_key:
                        continue
                    try:
                        t0 = time.perf_counter()
                        frames = ctx.decode(av.Packet(data))
                        self._stats_add("decode", time.perf_counter() - t0)
                    except av.error.FFmpegError as e:
                        log.debug("decode error: %s", e)
                        got_key = False  # wait for the next keyframe
                        newest = None
                        continue
                    if frames:
                        newest = frames[-1]
                # Frames often arrive in bursts (two in one read). Every one is decoded (the
                # next ones reference it) but only the newest is scaled and shown: showing the
                # older one too would only delay the current picture.
                if newest is not None:
                    t0 = time.perf_counter()
                    self._deliver(newest)
                    self._stats_add("deliver", time.perf_counter() - t0)
                    newest = None
        finally:
            resp.close()

    def _measure(self, frame):
        """Picture size without the black padding (grow-only per coded size, re-measured
        about once a second); also the visible corner of a collapsed frame."""
        w, h = frame.width, frame.height
        key = (w, h)
        y = np.frombuffer(frame.planes[0], np.uint8).reshape(h, frame.planes[0].line_size)[:, :w]
        rx, rkind = _content_extent(y, 1)
        by, bkind = _content_extent(y, 0)
        if rkind == "gray" and bkind == "gray":  # collapsed: picture in the top-left corner
            return rx, by
        now = time.monotonic()
        if key != self._content_key:
            self._content_key, self._content, self._measured = key, None, 0.0
        if self._content is None or now - self._measured >= MEASURE_EVERY_S:
            self._measured = now
            cw = rx if rkind == "pad" else (w if rkind == "none" else 0)
            ch = by if bkind == "pad" else (h if bkind == "none" else 0)
            old = self._content or (0, 0)
            self._content = (max(old[0], cw), max(old[1], ch))
        cw, ch = self._content
        return cw or w, ch or h

    # ---------------------------------------------------------------- screen off
    def _track_dark(self, frame):
        """When the iPhone screen turns off, the stream keeps coming as pure black frames.
        A dark frame alone could be a black video, so the screen only counts as off when the
        device also posted a screen-off/lock notification around the time it went dark
        (checked by _confirm_off a moment later)."""
        w, h = frame.width, frame.height
        y = np.frombuffer(frame.planes[0], np.uint8).reshape(h, frame.planes[0].line_size)[:, :w]
        dark = float(y[::24, ::24].mean()) < DARK_LUMA
        if dark == self._dark:
            return
        self._dark = dark
        if dark:
            self._dark_since = time.time()
            threading.Timer(1.2, self._confirm_off, args=(self._dark_since,)).start()
        elif self.screen_off:
            self.screen_off = False
            self.emit("screen_on")

    def _confirm_off(self, since):
        if not self._dark or self._dark_since != since or self.server is None:
            return
        try:
            events = self.server.extra("/state", timeout=3).get("events", [])
        except Exception as e:
            log.debug("state check failed: %s", e)
            return
        if any(since - 4 <= t <= since + 4 and name.endswith(("hasBlankedScreen", "lockcomplete"))
               for t, name in events):
            self.screen_off = True
            self.emit("screen_off")

    def wake(self):
        """Press the side button: turns a dark screen on (shows the Lock Screen)."""
        if self.server is None:
            return
        try:
            self.server.request("/button", json.dumps({"name": "lock", "state": "press"}).encode(), timeout=5).read()
        except Exception as e:
            log.info("wake failed: %s", e)

    def _deliver(self, frame):
        self._track_dark(frame)
        cw, ch = self._measure(frame)
        self._frames_in_session = getattr(self, "_frames_in_session", 0) + 1
        content = self._content or (cw, ch)
        full_w, full_h = (content[0] or frame.width), (content[1] or frame.height)
        self.content_size = (full_w, full_h)
        self.decoder.last_frame = _Frame(frame, full_w, full_h)
        # The window turns its portrait phone area into landscape when rotated, so decoder.target
        # is already the displayed (rotated) size. Render the upright content at the un-rotated
        # size, then rot90 it to fill that target. Touch is un-rotated in control._hid to match.
        rot = self.rotation % 360
        tw, th = self.decoder.target or (full_w, full_h)
        tw, th = max(16, int(tw)), max(16, int(th))
        ctw, cth = (th, tw) if rot in (90, 270) else (tw, th)  # content orientation
        if abs(ctw / cth - full_w / full_h) > 0.02:  # window not adapted to this aspect yet
            cth = max(16, round(ctw * full_h / full_w))
        # scale the whole picture so its visible part (cw x ch) fills ctw x cth, then cut that
        # part out: crops the padding and stretches a collapsed corner back
        sw = max(ctw, round(frame.width * ctw / cw))
        sh = max(cth, round(frame.height * cth / ch))
        img = frame.reformat(width=sw, height=sh, format="bgra", interpolation="AREA").to_ndarray()
        img = img[:cth, :ctw]
        if rot:
            img = np.rot90(img, {90: 3, 180: 2, 270: 1}[rot])  # 90 = clockwise
        img = np.ascontiguousarray(img)
        dw, dh = (full_h, full_w) if rot in (90, 270) else (full_w, full_h)  # displayed dims
        self.on_frame(img, dw, dh)

    # ---------------------------------------------------------------- audio
    def _audio_loop(self, session):
        if not self.settings.get("audio", True):
            return
        while not (self._stop.is_set() or session.is_set()):
            try:
                self._audio_once(session)
            except Exception as e:
                if self._stop.is_set() or session.is_set():
                    break
                log.info("audio stream: %s", e)
            if session.wait(1.5):
                break

    def _audio_once(self, session):
        server = self.server
        if server is None:
            return
        resp = server.request("/audio.bin", timeout=15)
        if self.player is None:
            self.player = Player(rate=AUDIO_RATE)
            self.player.muted = self.muted
        buf = bytearray()
        try:
            while not (self._stop.is_set() or session.is_set()):
                chunk = resp.read1(1 << 15)
                if not chunk:
                    raise ConnectionError("audio stream ended")
                buf += chunk
                while len(buf) >= 4:
                    n = int.from_bytes(buf[:4], "big")
                    if len(buf) < 4 + n:
                        break
                    pcm = bytes(buf[4:4 + n])
                    del buf[:4 + n]
                    if len(pcm) >= 4:
                        if not getattr(self, "_audio_logged", False):
                            self._audio_logged = True
                            log.info("audio: first PCM chunk, %d bytes", len(pcm))
                        self.player.write(np.frombuffer(pcm[: len(pcm) // 4 * 4], np.int16).reshape(-1, 2))
        finally:
            resp.close()


# ---------------------------------------------------------------------- errors
_HINTS = [
    (("developer mode",), "Chế độ nhà phát triển đang tắt. Bật trong Cài đặt > Quyền riêng tư & Bảo mật."),
    (("no device", "device is not connected", "connectionfailedtousbmuxd", "could not find device"),
     "Không thấy iPhone. Cắm cáp, mở khóa iPhone rồi thử lại."),
    (("password protected", "locked"), "iPhone đang khóa. Mở khóa rồi thử lại."),
    (("camera", "microphone"), "Một app trên iPhone đang dùng camera hoặc micro. Thoát app đó rồi thử lại."),
    (("ios 27", "refuse", "not supported"), "Chế độ này cần iOS 27 trở lên."),
    (("developerdiskimage", "mount"), "Chưa gắn được Developer Disk Image. Cắm cáp, mở khóa iPhone rồi thử lại."),
]


def explain(text: str) -> str:
    low = (text or "").lower()
    for keys, msg in _HINTS:
        if any(k in low for k in keys):
            return msg
    return ""
