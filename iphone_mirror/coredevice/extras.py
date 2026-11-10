"""Extra endpoints inside the server child, next to pymobiledevice3's serve-web.

They share the server's RSD tunnel to the iPhone (a second tunnel to the same device from
another process is not reliable), on their own loopback port with the same password:

  GET  /state             lock / screen state, from Darwin notifications seen so far
  POST /location          {"lat": .., "lon": ..}  simulate the GPS position
  POST /location/clear    back to the real GPS

Lock state: iOS 27.0 has neither CoreDevice getlockstate nor notify_get_state (27.2+), so
the state is followed from the notifications the device relays:
  com.apple.springboard.lockcomplete   posted when the device locks
  com.apple.springboard.lockstate      posted on lock AND unlock
  com.apple.springboard.hasBlankedScreen  posted when the screen turns off AND on
The app knows the screen was on and unlocked when mirroring started (it had video).

Location: the DVT LocationSimulation channel only takes latitude/longitude and the
simulated position lasts while the channel is open, so it is kept open here and the app
sends a new point for every step of a route.
"""
import asyncio
import base64
import hmac
import json
import logging
import os
import time

log = logging.getLogger("iphone_mirror.extras")

WATCHED = (
    "com.apple.springboard.lockcomplete",
    "com.apple.springboard.lockstate",
    "com.apple.springboard.hasBlankedScreen",
)


class Extras:
    def __init__(self, stream_server, password):
        self.srv = stream_server  # pymobiledevice3 ScreenStreamServer (has ._rsd)
        self.password = password or ""
        self.locked = False
        self.blanked = False
        self.events = []  # (time, name) of the last notifications
        self._last_lockcomplete = 0.0
        self._loc = None  # (DvtProvider, LocationSimulation) while simulating
        self._loc_lock = asyncio.Lock()
        self.location = None  # (lat, lon) last simulated point

    # ------------------------------------------------------------ notifications
    async def watch(self):
        from pymobiledevice3.services.notification_proxy import RemoteNotificationProxyService

        while True:
            try:
                async with RemoteNotificationProxyService(self.srv._rsd) as np:
                    for name in WATCHED:
                        await np.notify_register_dispatch(name)
                    log.info("watching lock/screen notifications")
                    async for ev in np.receive_notification():
                        name = ev.get("Name")
                        if name:
                            self._on_event(name)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.info("notification watch: %s", e)
            await asyncio.sleep(3)

    def _on_event(self, name):
        now = time.monotonic()
        self.events = (self.events + [(round(time.time(), 2), name)])[-12:]
        if name.endswith("lockcomplete"):
            self._last_lockcomplete = now
            self.locked = True
        elif name.endswith("lockstate"):
            # lock posts lockcomplete too (just before or after); decide a moment later
            asyncio.get_running_loop().call_later(0.8, self._settle_lockstate, now)
        elif name.endswith("hasBlankedScreen"):
            self.blanked = not self.blanked
            if not self.blanked and not self.locked:
                pass
        log.info("event %s -> locked=%s blanked=%s", name, self.locked, self.blanked)

    def _settle_lockstate(self, at):
        self.locked = abs(self._last_lockcomplete - at) < 1.5
        if not self.locked:
            self.blanked = False  # unlocking needs a lit screen
        log.info("lockstate settled -> locked=%s blanked=%s", self.locked, self.blanked)

    # ------------------------------------------------------------ location
    async def set_location(self, lat, lon):
        from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
        from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation

        async with self._loc_lock:
            for attempt in (1, 2):
                if self._loc is None:
                    dvt = DvtProvider(self.srv._rsd)
                    await dvt.__aenter__()
                    sim = LocationSimulation(dvt)
                    try:
                        await sim.__aenter__()
                    except BaseException:
                        await dvt.__aexit__(None, None, None)
                        raise
                    self._loc = (dvt, sim)
                try:
                    await self._loc[1].set(lat, lon)
                    self.location = (lat, lon)
                    return
                except Exception:
                    if attempt == 2:
                        raise
                    await self._close_location()  # stale channel: reopen once

    async def clear_location(self):
        async with self._loc_lock:
            if self._loc is not None:
                try:
                    await self._loc[1].clear()
                except Exception as e:
                    log.info("clear location: %s", e)
            await self._close_location()
            self.location = None

    async def _close_location(self):
        loc, self._loc = self._loc, None
        if loc is None:
            return
        dvt, sim = loc
        for cm in (sim, dvt):
            try:
                await cm.__aexit__(None, None, None)
            except Exception:
                pass

    # ------------------------------------------------------------ HTTP
    def _authorized(self, headers):
        scheme, _, cred = headers.get("authorization", "").partition(" ")
        if scheme.lower() != "basic":
            return False
        try:
            _, _, pw = base64.b64decode(cred.strip()).decode("utf-8").partition(":")
        except Exception:
            return False
        return hmac.compare_digest(pw.encode(), self.password.encode())

    async def handle(self, reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method, path = (lines[0].split(" ") + ["", ""])[:2]
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            n = int(headers.get("content-length") or 0)
            body = await reader.readexactly(n) if n else b""
            if headers.get("origin") or not self._authorized(headers):
                status, out = "403 Forbidden", {"error": "forbidden"}
            else:
                status, out = await self._route(method, path, body)
        except Exception as e:
            log.exception("extras request failed")
            status, out = "500 Internal Server Error", {"error": str(e)}
        data = json.dumps(out).encode()
        writer.write(
            f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\n"
            "Connection: close\r\n\r\n".encode() + data
        )
        try:
            await writer.drain()
        finally:
            writer.close()

    async def _route(self, method, path, body):
        if method == "GET" and path == "/state":
            return "200 OK", {
                "locked": self.locked, "blanked": self.blanked, "events": self.events,
                "location": self.location,
            }
        if method == "POST" and path == "/location":
            data = json.loads(body or b"{}")
            await self.set_location(float(data["lat"]), float(data["lon"]))
            return "200 OK", {"ok": True}
        if method == "POST" and path == "/location/clear":
            await self.clear_location()
            return "200 OK", {"ok": True}
        return "404 Not Found", {"error": "not found"}


def install():
    """Start the extras next to ScreenStreamServer.serve() (port and password from the env)."""
    from pymobiledevice3.remote.core_device.screen_stream import ScreenStreamServer

    port = int(os.environ.get("IPHONE_MIRROR_EXTRA_PORT") or 0)
    if not port:
        return
    password = os.environ.get("IPHONE_MIRROR_PASSWORD", "")
    original = ScreenStreamServer.serve

    async def serve(self):
        extras = Extras(self, password)
        server = await asyncio.start_server(extras.handle, "127.0.0.1", port)
        watcher = asyncio.create_task(extras.watch())
        log.info("extras listening on 127.0.0.1:%d", port)
        try:
            return await original(self)
        finally:
            watcher.cancel()
            server.close()
            try:
                await extras.clear_location()
            except Exception:
                pass

    ScreenStreamServer.serve = serve
