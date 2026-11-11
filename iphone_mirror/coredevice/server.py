"""The device-side server: pymobiledevice3 ``serve-web`` in a child process.

Run as ``python -m iphone_mirror.coredevice.server [serve-web options]``: it patches
pymobiledevice3 for Windows, then starts its CLI. ServerProcess starts and stops that
child from the app and knows its URL and password.

Patches (applied in the child only, nothing in site-packages is changed):
  * audio: pymobiledevice3 decodes the AAC-ELD sound with macOS AudioToolbox. On other
    systems FFmpeg (PyAV) decodes it instead, to the same s16le 48 kHz stereo PCM.
"""
import base64
import json
import logging
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from ..config import APP_DIR

log = logging.getLogger(__name__)

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LOG_FILE = APP_DIR / "coredevice_server.log"


# ---------------------------------------------------------------------- child side
class PyAVAacEldDecoder:
    """AAC-ELD -> s16le 48 kHz stereo PCM with FFmpeg; same interface as
    pymobiledevice3's AudioToolbox AACELDDecoder (one access unit per decode call)."""

    def __init__(self, magic_cookie: bytes = bytes([0xF8, 0xE6, 0x40, 0x00])):
        import av

        self._av = av
        # The cookie pymobiledevice3 passes (F8 E6 40 00) has frameLengthFlag = 0, i.e.
        # 512-sample frames; AudioToolbox is forced to 480 separately, but FFmpeg trusts
        # the AudioSpecificConfig. The device sends 480-sample frames: set the flag
        # (bit 19 of the config) -> F8 E6 50 00.
        cookie = bytearray(magic_cookie)
        if len(cookie) >= 3:
            cookie[2] |= 0x10
        self._ctx = av.CodecContext.create("aac", "r")
        self._ctx.extradata = bytes(cookie)
        # without these FFmpeg's ER AAC ELD path fails every packet ("Internal bug")
        self._ctx.sample_rate = 48000
        try:
            self._ctx.layout = "stereo"
        except Exception:
            pass
        self._resampler = av.AudioResampler(format="s16", layout="stereo", rate=48000)

    def decode(self, au: bytes) -> bytes:
        out = []
        for frame in self._ctx.decode(self._av.Packet(au)):
            for r in self._resampler.resample(frame):
                out.append(r.to_ndarray().tobytes())
        return b"".join(out)


def _patch_bonjour_browse():
    """Discover Bonjour/mDNS services with the `zeroconf` library instead of
    pymobiledevice3's own multicast socket code.

    pymobiledevice3 rolls its own mDNS querier (pymobiledevice3.bonjour). On this
    Windows PC it never gets any answers back (multiple interfaces / Windows multicast
    quirks), so `iter_browse_service` yields nothing and the iPhone's `_remotepairing._tcp`
    service is never seen even while it is clearly advertising. `zeroconf` picks the right
    interfaces and finds it in a few seconds. We yield the same `ServiceInstance` objects
    the rest of the stack expects, so `get_remote_pairing_tunnel_services` keeps working."""
    try:
        from zeroconf import ServiceBrowser, Zeroconf
    except Exception:
        logging.getLogger("iphone_mirror.wifi").warning(
            "zeroconf not installed; Wi-Fi discovery falls back to pymobiledevice3's own mDNS"
        )
        return
    import asyncio

    from pymobiledevice3 import bonjour
    from pymobiledevice3.bonjour import Address, ServiceInstance
    from pymobiledevice3.remote import tunnel_service

    def _blocking_browse(service_type, timeout):
        service_type = service_type if service_type.endswith(".") else service_type + "."
        out, lock = [], threading.Lock()

        class _Listener:
            def add_service(self, zc, type_, name):
                info = zc.get_service_info(type_, name, timeout=3000)
                if not info:
                    return
                props = {}
                for k, v in (info.properties or {}).items():
                    try:
                        key = k.decode() if isinstance(k, bytes) else str(k)
                        val = v.decode(errors="replace") if isinstance(v, bytes) else ("" if v is None else str(v))
                    except Exception:
                        continue
                    props[key] = val
                # IPv4 is enough to reach the device on the LAN; skip IPv6 scope juggling.
                addrs = [Address(ip=ip, iface="") for ip in info.parsed_addresses() if ":" not in ip]
                if not addrs:
                    return
                with lock:
                    out.append(ServiceInstance(
                        instance=name, host=info.server, port=info.port or 0,
                        addresses=addrs, properties=props,
                    ))

            def update_service(self, *a):
                pass

            def remove_service(self, *a):
                pass

        zc = Zeroconf()
        try:
            ServiceBrowser(zc, service_type, _Listener())
            time.sleep(timeout)
        finally:
            zc.close()
        with lock:
            return list(out)

    async def iter_browse_service(service_type, timeout=4.0):
        loop = asyncio.get_running_loop()
        instances = await loop.run_in_executor(None, _blocking_browse, service_type, timeout)
        seen = set()
        for inst in instances:
            key = (inst.instance, inst.port)
            if key in seen or not inst.addresses:
                continue
            seen.add(key)
            yield inst

    bonjour.iter_browse_service = iter_browse_service
    tunnel_service.iter_browse_service = iter_browse_service
    logging.getLogger("iphone_mirror.wifi").info("Bonjour discovery routed through zeroconf")


def _patch_wifi_tunnel():
    """Reach the iPhone over Wi-Fi when no cable is in.

    With a cable the no-root tunnel uses the CoreDeviceProxy lockdown service through usbmuxd.
    That service is USB-only: over a Wi-Fi lockdown connection the device accepts it and then
    drops the tunnel handshake. Over Wi-Fi iOS 17+ tunnels go through RemotePairing
    (_remotepairing._tcp, what Xcode uses), which needs a RemotePairing record for this PC,
    created once over USB with `lockdown remotepairing --pair` (see pair_remote()). So when
    usbmuxd has no USB device, browse for the paired iPhone's RemotePairing service and
    build the same userspace tunnel on it."""
    from pymobiledevice3 import usbmux
    from pymobiledevice3.remote import tunnel_service, userspace_tunnel

    original = userspace_tunnel._create_no_root_tunnel_provider
    log_ = logging.getLogger("iphone_mirror.wifi")

    async def provider(serial, autopair, remotepairing_fallback=True):
        try:
            devices = await usbmux.list_devices()
        except Exception:
            devices = []
        if any(d.connection_type == "USB" and (serial is None or d.matches_udid(serial)) for d in devices):
            return await original(serial, autopair, remotepairing_fallback)
        target = serial or os.environ.get("IPHONE_MIRROR_UDID") or None
        log_.info("no USB device; looking for %s over Wi-Fi (RemotePairing)", target or "a paired iPhone")
        # 2 s (the library default) is too short here: the zeroconf browse plus the
        # per-service TXT/SRV resolve needs a few seconds on this PC's network.
        services = await tunnel_service.get_remote_pairing_tunnel_services(udid=target, bonjour_timeout=8)
        if not services:
            raise ConnectionError(
                "no paired iPhone found over Wi-Fi (RemotePairing). Plug the cable in once and use "
                "'Bật dùng qua Wi-Fi', keep the iPhone unlocked and on the same network."
            )
        for extra in services[1:]:
            try:
                await extra.close()
            except Exception:
                pass
        log_.info("RemotePairing service found")
        return services[0], None

    userspace_tunnel._create_no_root_tunnel_provider = provider


def _patch_tunnel_socket():
    """Low-latency TCP options on the socket that carries the Wi-Fi tunnel (see perf.py)."""
    from pymobiledevice3.remote import tunnel_service

    from .perf import tune_tunnel_socket

    original = tunnel_service.create_connection

    def create_connection(*args, **kwargs):
        sock = original(*args, **kwargs)
        tune_tunnel_socket(sock)
        return sock

    tunnel_service.create_connection = create_connection


STALL_NO_VIDEO_S = 5.0  # no video at least this long ...
STALL_UNANSWERED_S = 4.0  # ... and the last input got no picture change this long: restart


def _patch_stall_watchdog():
    """Restart the stream only when the user's input got no answer, never on a still screen.

    A still screen sends no video at all. pymobiledevice3's watchdog restarted the stream
    after 5 s without video, and a restart also closes the HID services and empties the
    input queue: a swipe made around then was lost or cut (measured: a restart every
    ~15 s while the screen stood still). Asking for a keyframe does not help: a still
    encoder ignores the request. So the stream counts as stalled only if a touch, key or
    button came after the last picture and nothing changed STALL_UNANSWERED_S later
    (and never while a finger is down)."""
    import asyncio
    import json as _json

    from pymobiledevice3.remote.core_device import screen_stream as ss

    log_ = logging.getLogger("iphone_mirror.stall")
    cls = ss.ScreenStreamServer

    def note_input(self, touching=None):
        self._im_last_input_t = asyncio.get_running_loop().time()
        if touching is not None:
            self._im_touch_down = touching

    orig_touch, orig_key, orig_button = cls._handle_touch, cls._handle_key, cls._handle_button

    async def handle_touch(self, body):
        try:
            op = _json.loads(body).get("type")
        except Exception:
            op = None
        note_input(self, touching=(op == "contact"))
        return await orig_touch(self, body)

    async def handle_key(self, body):
        note_input(self)
        return await orig_key(self, body)

    async def handle_button(self, body):
        note_input(self)
        return await orig_button(self, body)

    async def stall_watchdog(self):
        loop = asyncio.get_running_loop()
        while True:
            try:
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                return
            if not self._subscribers or self._active_service is None:
                continue
            now = loop.time()
            last_video = self._last_good_au_t
            last_input = getattr(self, "_im_last_input_t", 0.0)
            if (
                now - last_video < STALL_NO_VIDEO_S
                or last_input <= last_video
                or now - last_input < STALL_UNANSWERED_S
                or getattr(self, "_im_touch_down", False)
                or now - self._last_restart_t < ss._STALL_RESTART_COOLDOWN_SECS
            ):
                continue
            log_.warning("input %.1fs ago got no picture change (no video for %.1fs) - restarting stream",
                         now - last_input, now - last_video)
            self._last_restart_t = now
            self._im_last_input_t = 0.0  # one restart per unanswered input
            try:
                await asyncio.wait_for(self._ensure_fresh_stream(force=True), timeout=10.0)
            except asyncio.TimeoutError:
                log_.warning("stream restart did not complete within 10s")
            except Exception as exc:
                if ss._is_tunnel_dead_error(exc):
                    log_.warning("device appears disconnected (%s); handing off to reconnect loop", exc)
                    self._reconnect_signal.set()
                else:
                    log_.exception("stream restart failed")

    cls._handle_touch, cls._handle_key, cls._handle_button = handle_touch, handle_key, handle_button
    cls._stall_watchdog = stall_watchdog


def apply_patches():
    if sys.platform == "darwin":
        return
    from pymobiledevice3.remote.core_device import screen_stream

    screen_stream.AACELDDecoder = PyAVAacEldDecoder
    screen_stream.ScreenStreamServer._missing_audio_deps = staticmethod(lambda: [])
    _patch_bonjour_browse()
    _patch_wifi_tunnel()
    _patch_tunnel_socket()
    _patch_stall_watchdog()
    from . import extras

    extras.install()


def child_main(argv):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    from .perf import boost_process

    boost_process()
    apply_patches()
    from pymobiledevice3.__main__ import main as pmd3_main

    sys.argv = ["pymobiledevice3", "developer", "core-device", "display", "serve-web", *argv]
    return pmd3_main()


# ---------------------------------------------------------------------- app side
def _pmd3(*cli, timeout=60):
    args = [_python(), "-m", "pymobiledevice3", *cli]
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", NO_COLOR="1")
    p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, env=env, creationflags=NO_WINDOW)
    out = (p.stdout + p.stderr).strip()
    log.info("pymobiledevice3 %s -> %d: %s", " ".join(cli), p.returncode, out[-500:])
    return p.returncode, out


def enable_wifi():
    """Make the iPhone reachable over Wi-Fi from this PC. Needs the cable once:
      1. lockdown EnableWifiConnections (the device answers this host on the network),
      2. a RemotePairing record (`lockdown remotepairing --pair`, promptless over the already
         trusted USB connection): Wi-Fi tunnels go through RemotePairing.
    Returns (ok, message)."""
    try:
        code, out = _pmd3("lockdown", "wifi-connections", "on")
        if code != 0:
            if "not connected" in out.lower() or "no device" in out.lower():
                return False, "Cần cắm cáp USB (iPhone mở khóa) để bật dùng qua Wi-Fi."
            return False, out.splitlines()[-1] if out else f"lỗi {code}"
        code, out = _pmd3("lockdown", "remotepairing", "--pair", timeout=90)
        if code != 0:
            return False, "Ghép đôi Wi-Fi chưa được: " + (out.splitlines()[-1] if out else f"lỗi {code}")
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return True, "Đã bật. Rút cáp ra là dùng qua Wi-Fi được (cùng mạng Wi-Fi, iPhone mở khóa)."


def _python():
    """python.exe for the child (pythonw.exe has no usable stdout); no console window."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        return str(exe.with_name("python.exe"))
    return str(exe)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerProcess:
    """Starts the serve-web child on a free loopback port with a random password."""

    def __init__(self, ltrp=False, audio=True, udid=None, motion_idr=False):
        self.ltrp = ltrp
        # Forced keyframes while the screen moves bring a collapsed (blurry) picture back to
        # full resolution faster, but measured on Wi-Fi they halve the frame rate (~23 vs
        # ~50 fps): off by default, the window stretches a collapsed picture instead.
        self.motion_idr = motion_idr
        self.audio = audio
        self.udid = udid  # the iPhone to look for over Wi-Fi (learned over USB)
        self.port = 0
        self.extra_port = 0  # extras.py: lock state, GPS simulation
        self.password = secrets.token_urlsafe(18)
        self.proc = None
        self._log = None
        self._lock = threading.Lock()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"

    @property
    def auth_header(self):
        token = base64.b64encode(f"app:{self.password}".encode()).decode()
        return "Basic " + token

    def start(self):
        with self._lock:
            self.stop_locked()
            self.port = _free_port()
            self.extra_port = _free_port()
            args = [
                _python(), "-m", "iphone_mirror.coredevice.server",
                "--bind", "127.0.0.1", "--http-port", str(self.port), "--password", self.password,
            ]
            if self.ltrp:
                args.append("--ltrp")
            args.append("--motion-idr" if self.motion_idr else "--no-motion-idr")
            if not self.audio:
                args.append("--no-audio")
            APP_DIR.mkdir(parents=True, exist_ok=True)
            self._log = open(LOG_FILE, "w", encoding="utf-8", errors="replace")
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1", NO_COLOR="1")
            if self.udid:
                env["IPHONE_MIRROR_UDID"] = self.udid
            env["IPHONE_MIRROR_EXTRA_PORT"] = str(self.extra_port)
            env["IPHONE_MIRROR_PASSWORD"] = self.password
            # run from the package's parent folder so "-m iphone_mirror..." resolves
            cwd = str(Path(__file__).resolve().parents[2])
            log.info("starting CoreDevice server on port %d (ltrp=%s, motion_idr=%s)",
                     self.port, self.ltrp, self.motion_idr)
            self.proc = subprocess.Popen(
                args, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=self._log, stderr=subprocess.STDOUT,
                creationflags=NO_WINDOW,
            )

    def request(self, path, data=None, timeout=5, method=None):
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", self.auth_header)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        return urllib.request.urlopen(req, timeout=timeout)

    def extra(self, path, payload=None, timeout=10):
        """Call an extras.py endpoint; returns the decoded JSON reply."""
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.extra_port}{path}", data=data,
                                     method="POST" if data is not None else "GET")
        req.add_header("Authorization", self.auth_header)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def wait_ready(self, timeout=90, cancel=None):
        """Wait until /codec answers 200 (the device stream is up). Returns None on success,
        else a short reason."""
        deadline = time.monotonic() + timeout
        last = "starting"
        while time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                return "cancelled"
            if not self.alive():
                return "exited"
            try:
                with self.request("/codec", timeout=8) as r:
                    if r.status == 200:
                        return None
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}"
            except OSError as e:
                last = str(e)
            time.sleep(0.7)
        return f"timeout ({last})"

    def log_tail(self, n=4000):
        try:
            return LOG_FILE.read_text("utf-8", "replace")[-n:]
        except OSError:
            return ""

    def stop(self):
        with self._lock:
            self.stop_locked()

    def stop_locked(self):
        p, self.proc = self.proc, None
        if p is not None and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        if self._log is not None:
            self._log.close()
            self._log = None


if __name__ == "__main__":
    sys.exit(child_main(sys.argv[1:]))
