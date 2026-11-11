"""Input for the CoreDevice server: live touch, keyboard, hardware buttons, pasteboard.

Touch is sent as it happens (contact samples while the mouse button is held, then a
release), so drags follow the pointer like on a real screen. Requests go out in order
over one keep-alive connection per sender thread; the server answers at once and
replays them to the device in the same order.

LiveControl also offers the small part of control.Controller's interface that
PhoneWindow uses (state, snapshot, active, start, stop, home, app_switcher, button).
"""
import collections
import http.client
import json
import logging
import os
import queue
import threading
import time

log = logging.getLogger(__name__)

OFF, STARTING, READY, ERROR = "off", "starting", "ready", "error"
HID_MAX = 65535
MOVE_INTERVAL_S = 0.008  # contact samples while dragging are sent at most this often

# Windows virtual-key code -> USB HID keyboard usage (layout-independent physical keys)
VK_TO_HID = {}
for _i in range(26):
    VK_TO_HID[0x41 + _i] = 0x04 + _i  # A..Z
for _i in range(1, 10):
    VK_TO_HID[0x30 + _i] = 0x1D + _i  # 1..9
VK_TO_HID[0x30] = 0x27  # 0
VK_TO_HID.update({
    0x0D: 0x28, 0x1B: 0x29, 0x08: 0x2A, 0x09: 0x2B, 0x20: 0x2C,  # Enter Esc Backspace Tab Space
    0xBD: 0x2D, 0xBB: 0x2E, 0xDB: 0x2F, 0xDD: 0x30, 0xDC: 0x31,  # - = [ ] \
    0xBA: 0x33, 0xDE: 0x34, 0xC0: 0x35, 0xBC: 0x36, 0xBE: 0x37, 0xBF: 0x38,  # ; ' ` , . /
    0x14: 0x39,  # Caps Lock
    0x27: 0x4F, 0x25: 0x50, 0x28: 0x51, 0x26: 0x52,  # arrows
    0x2E: 0x4C, 0x24: 0x4A, 0x23: 0x4D, 0x21: 0x4B, 0x22: 0x4E,  # Delete Home End PgUp PgDn
    0x10: 0xE1, 0xA0: 0xE1, 0xA1: 0xE5,  # Shift
    0x11: 0xE0, 0xA2: 0xE0, 0xA3: 0xE4,  # Ctrl
    0x12: 0xE2, 0xA4: 0xE2, 0xA5: 0xE6,  # Alt
    0x5B: 0xE3, 0x5C: 0xE7,  # Windows key -> Command
})
for _i in range(12):
    VK_TO_HID[0x70 + _i] = 0x3A + _i  # F1..F12
HID_LEFT_GUI, HID_V, HID_LEFT_SHIFT, HID_BACKSPACE = 0xE3, 0x19, 0xE1, 0x2A
MODIFIER_USAGES = frozenset(range(0xE0, 0xE8))  # Ctrl Shift Alt Command, left and right

# Printable ASCII -> (HID usage, needs Shift) on a US layout: text an input method sends
# as Unicode packets (EVKey re-sends "ng" after "ế") is typed as keys when it is plain ASCII.
ASCII_TO_HID = {" ": (0x2C, False), "\n": (0x28, False), "\r": (0x28, False), "\t": (0x2B, False)}
for _i in range(26):
    ASCII_TO_HID[chr(0x61 + _i)] = (0x04 + _i, False)
    ASCII_TO_HID[chr(0x41 + _i)] = (0x04 + _i, True)
for _i, (_plain, _shifted) in enumerate(zip("1234567890", "!@#$%^&*()")):
    ASCII_TO_HID[_plain] = (0x1E + _i, False)
    ASCII_TO_HID[_shifted] = (0x1E + _i, True)
for _usage, _plain, _shifted in (
    (0x2D, "-", "_"), (0x2E, "=", "+"), (0x2F, "[", "{"), (0x30, "]", "}"), (0x31, "\\", "|"),
    (0x33, ";", ":"), (0x34, "'", '"'), (0x35, "`", "~"), (0x36, ",", "<"), (0x37, ".", ">"), (0x38, "/", "?"),
):
    ASCII_TO_HID[_plain] = (_usage, False)
    ASCII_TO_HID[_shifted] = (_usage, True)

TRACE_INPUT = bool(os.environ.get("IPHONE_MIRROR_TRACE_INPUT"))  # log every key/paste sent
PASTE_SETTLE_S = 0.05  # after the pasteboard is set, before Command+V
PASTE_APPLY_S = 0.25  # after Command+V, before the next key (0.1 s still mixed up the order)
RESTORE_CLIPBOARD_S = 1.5  # typing pause after which the iPhone pasteboard is put back
_UNSET = object()


class _Sender(threading.Thread):
    """Posts JSON requests in order over one keep-alive HTTP connection."""

    def __init__(self, name, ctl):
        super().__init__(name=name, daemon=True)
        self.ctl = ctl
        self.q = queue.Queue()
        self._conn = None

    def put(self, path, payload):
        self.q.put((path, payload))

    def stop(self):
        self.q.put(None)

    def run(self):
        while True:
            item = self.q.get()
            if item is None:
                break
            path, payload = item
            if callable(payload):  # deferred work (e.g. a wait between two requests)
                try:
                    payload()
                except Exception:
                    log.exception("deferred control step failed")
                continue
            self._post(path, payload)
        if self._conn is not None:
            self._conn.close()

    def _post(self, path, payload, retry=True):
        server = self.ctl.server
        if server is None:
            return
        body = json.dumps(payload).encode()
        if TRACE_INPUT:
            log.info("send %s %s", path, body.decode("utf-8", "replace"))
        try:
            if self._conn is None:
                self._conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
            self._conn.request("POST", path, body, {
                "Content-Type": "application/json", "Authorization": server.auth_header,
                "Connection": "keep-alive",
            })
            r = self._conn.getresponse()
            r.read()
            if r.status != 200:
                log.warning("POST %s -> %d", path, r.status)
        except (OSError, http.client.HTTPException) as e:
            if self._conn is not None:
                self._conn.close()
            self._conn = None
            if retry:
                self._post(path, payload, retry=False)
            else:
                log.info("POST %s failed: %s", path, e)


class LiveControl:
    def __init__(self, on_state=lambda state, message: None):
        self.on_state = on_state
        self.server = None
        self.state = OFF
        self.message = ""
        self._serial = 0
        self.screen_aspect = None  # set by PhoneWindow (unused: touch is normalised)
        self.rotation = 0  # must match CoreDeviceReceiver.rotation: un-rotate touch back to the iPhone
        self.device_ip = None
        self._touch = _Sender("touch", self)
        self._other = _Sender("control", self)
        self._touch.start()
        self._other.start()
        self._last_move = 0.0
        self._pending_move = None
        self._move_timer = None
        self._lock = threading.Lock()
        self._keys = []  # HID usages currently held, in press order
        self.clipboard_sync = None  # ClipboardSync, set by the app
        self._clip_saved = _UNSET  # iPhone pasteboard text from before input-method typing
        self._restore_timer = None

    # ---------------------------------------------------------------- state
    @property
    def active(self):
        return self.state in (STARTING, READY)

    def snapshot(self):
        with self._lock:
            return self.state, self.message, self._serial

    def _set(self, state, message=""):
        with self._lock:
            self.state, self.message = state, message
            self._serial += 1
        try:
            self.on_state(state, message)
        except Exception:
            log.exception("on_state failed")

    def attach(self, server):
        """The server is up: input can flow."""
        self.server = server
        self._set(READY, "")

    def detach(self, message=""):
        self.server = None
        self._set(ERROR if message else OFF, message)

    def start(self):  # control is always on in this mode
        pass

    def stop(self):
        pass

    def close(self):
        self._touch.stop()
        self._other.stop()

    # ---------------------------------------------------------------- touch
    def _hid(self, xf, yf):
        # The view is the iPhone content turned clockwise by self.rotation; a click is in that
        # displayed frame, so rotate it back into the iPhone's own coordinates before sending.
        r = self.rotation % 360
        if r == 90:
            xf, yf = yf, 1 - xf
        elif r == 180:
            xf, yf = 1 - xf, 1 - yf
        elif r == 270:
            xf, yf = 1 - yf, xf
        return (max(0, min(HID_MAX, round(xf * HID_MAX))), max(0, min(HID_MAX, round(yf * HID_MAX))))

    def touch_down(self, xf, yf):
        x, y = self._hid(xf, yf)
        self._pending_move = None
        self._touch.put("/touch", {"type": "contact", "x": x, "y": y})
        self._last_move = time.monotonic()

    def touch_move(self, xf, yf):
        """Contact samples while dragging, at most one per MOVE_INTERVAL_S (the newest wins)."""
        x, y = self._hid(xf, yf)
        now = time.monotonic()
        if now - self._last_move >= MOVE_INTERVAL_S:
            self._last_move = now
            self._pending_move = None
            self._touch.put("/touch", {"type": "contact", "x": x, "y": y})
        else:
            self._pending_move = (x, y)

    def touch_up(self, xf, yf):
        if self._pending_move is not None:
            px, py = self._pending_move
            self._pending_move = None
            self._touch.put("/touch", {"type": "contact", "x": px, "y": py})
        x, y = self._hid(xf, yf)
        self._touch.put("/touch", {"type": "release", "x": x, "y": y})

    def flush_move(self):
        """Send a held-back drag sample (called by a timer while the pointer rests)."""
        if self._pending_move is not None:
            x, y = self._pending_move
            self._pending_move = None
            self._last_move = time.monotonic()
            self._touch.put("/touch", {"type": "contact", "x": x, "y": y})

    def swipe(self, x0, y0, x1, y1, seconds=0.25, steps=12, hold_end=0.06):
        """A whole drag in the background (wheel scrolling, App Switcher)."""
        def run():
            self._touch._post("/touch", {"type": "contact", "x": self._hid(x0, y0)[0], "y": self._hid(x0, y0)[1]})
            for i in range(1, steps + 1):
                time.sleep(seconds / steps)
                x, y = self._hid(x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps)
                self._touch._post("/touch", {"type": "contact", "x": x, "y": y})
            if hold_end:
                time.sleep(hold_end)
            x, y = self._hid(x1, y1)
            self._touch._post("/touch", {"type": "release", "x": x, "y": y})
        self._touch.put("/touch", run)

    # ---------------------------------------------------------------- keyboard
    def _drop_held_letters(self):
        """Treat held non-modifier keys as released on the iPhone, without pressing them again.

        Fast typing overlaps keys: "y" is still down when the input method eats "f" and sends
        Backspaces + "ày". Sending Backspace as [y, Backspace], or restoring [y] after a paste,
        makes iOS type another "y". The PC still releases "y" later; key_up then has nothing
        left to do."""
        if any(u not in MODIFIER_USAGES for u in self._keys):
            self._keys = [u for u in self._keys if u in MODIFIER_USAGES]
            self._other.put("/key", {"usages": list(self._keys)})

    def key_down(self, usage):
        if usage == HID_BACKSPACE:
            self._drop_held_letters()
        if usage not in self._keys:
            self._keys.append(usage)
            self._other.put("/key", {"usages": list(self._keys)})

    def key_up(self, usage):
        if usage in self._keys:
            self._keys.remove(usage)
            self._other.put("/key", {"usages": list(self._keys)})

    def release_keys(self):
        if self._keys:
            self._keys = []
            self._other.put("/key", {"usages": []})

    def type_unicode(self, text):
        """Text from an input method (EVKey, Unikey, the emoji panel...): plain ASCII is typed
        as keys, anything else is pasted in one go and the iPhone pasteboard is put back
        afterwards (see paste_text)."""
        if not text:
            return
        self._drop_held_letters()
        if all(c in ASCII_TO_HID for c in text):
            self._type_ascii(text)
        else:
            self.paste_text(text, restore=True)

    def _type_ascii(self, text):
        """US-layout key presses for each character, whatever modifiers are held right now;
        the held keys are pressed again at the end."""
        for ch in text:
            usage, shift = ASCII_TO_HID[ch]
            if shift:
                self._other.put("/key", {"usages": [HID_LEFT_SHIFT]})
                self._other.put("/key", {"usages": [HID_LEFT_SHIFT, usage]})
            else:
                self._other.put("/key", {"usages": [usage]})
            self._other.put("/key", {"usages": []})
        if self._keys:
            self._other.put("/key", {"usages": list(self._keys)})

    def paste_text(self, text, restore=False):
        """Type any text (Vietnamese, emoji...) by putting it on the iPhone pasteboard and
        pressing Command+V: a hardware keyboard can only send keys of the iPhone's layout.

        restore=True (typing through an input method): what was on the iPhone pasteboard
        before is read once and put back RESTORE_CLIPBOARD_S after the last such paste, so
        typing Vietnamese does not leave "ế" where the user's copied text was."""
        self._drop_held_letters()
        if restore:
            self._other.put("/clipboard", self._save_clipboard_once)
        if self.clipboard_sync is not None:
            self.clipboard_sync.ignore_next_device_text(text)
        self._other.put("/clipboard", {"text": text})
        self._other.put("/key", lambda: time.sleep(PASTE_SETTLE_S))
        self._other.put("/key", {"usages": [HID_LEFT_GUI]})
        self._other.put("/key", {"usages": [HID_LEFT_GUI, HID_V]})
        self._other.put("/key", {"usages": []})
        # iOS pastes asynchronously: keys sent sooner can land before the pasted text
        self._other.put("/key", lambda: time.sleep(PASTE_APPLY_S))
        if self._keys:
            self._other.put("/key", {"usages": list(self._keys)})
        if restore:
            self._schedule_restore()

    def tap(self, usage, times=1):
        """Press and release a key (Backspace for the word composer) without modifiers;
        held modifiers are pressed again afterwards."""
        self._drop_held_letters()
        for _ in range(times):
            self._other.put("/key", {"usages": [usage]})
            self._other.put("/key", {"usages": []})
        if self._keys:
            self._other.put("/key", {"usages": list(self._keys)})

    # Runs on the "control" sender thread, in order with the pastes.
    def _save_clipboard_once(self):
        if self._clip_saved is not _UNSET or self.server is None:
            return
        saved = None
        try:
            with self.server.request("/clipboard", timeout=5) as r:
                saved = json.loads(r.read()).get("text")
        except Exception as e:
            log.debug("read pasteboard before typing: %s", e)
        self._clip_saved = saved  # None: nothing to put back (empty, image, or unreadable)

    def _schedule_restore(self):
        with self._lock:
            if self._restore_timer is not None:
                self._restore_timer.cancel()
            t = threading.Timer(RESTORE_CLIPBOARD_S, lambda: self._other.put("/clipboard", self._restore_clipboard))
            t.daemon = True
            self._restore_timer = t
            t.start()

    def _restore_clipboard(self):
        saved, self._clip_saved = self._clip_saved, _UNSET
        if saved is None or saved is _UNSET or self.server is None:
            return
        if self.clipboard_sync is not None:
            self.clipboard_sync.ignore_next_device_text(saved)
        self._other._post("/clipboard", {"text": saved})

    # ---------------------------------------------------------------- buttons (Controller API)
    def press(self, name):
        """name: home, lock, volume-up, volume-down, mute, siri."""
        self._other.put("/button", {"name": name, "state": "press"})

    def home(self):
        self.press("home")

    def app_switcher(self):
        # Double press of the Home button usage. Tested on iOS 27 (iPhone 14 Pro Max): the
        # swipe-up-and-hold gesture from the bottom edge does NOT open the App Switcher with
        # injected touches (system edge gestures ignore them), the double Home press does.
        # Both presses are queued back to back: the server's HID worker plays them one right
        # after the other (~50 ms apart), well inside iOS's double-press window. Waiting on
        # this side between them added network jitter and sometimes missed the window.
        self.press("home")
        self.press("home")

    def button(self, name):
        self.press({"volumeUp": "volume-up", "volumeDown": "volume-down"}.get(name, name))

    def set_clipboard(self, text):
        self._other.put("/clipboard", {"text": text})


class ClipboardSync(threading.Thread):
    """Two-way pasteboard: device copies -> PC clipboard (long poll), PC copies -> device.

    on_device_text(text) is called on this thread; the app hands it to the GUI thread."""

    def __init__(self, ctl, on_device_text):
        super().__init__(name="clipboard", daemon=True)
        self.ctl = ctl
        self.on_device_text = on_device_text
        self.enabled = threading.Event()
        self._stop = threading.Event()
        self._ignore = collections.deque(maxlen=4)  # texts we put there ourselves

    def ignore_next_device_text(self, text):
        self._ignore.append(text)

    def stop(self):
        self._stop.set()

    def run(self):
        seq = None
        while not self._stop.is_set():
            if not self.enabled.wait(1.0):
                seq = None
                continue
            server = self.ctl.server
            if server is None:
                time.sleep(1)
                continue
            try:
                path = "/clipboard/events" + (f"?since={seq}" if seq is not None else "")
                with server.request(path, timeout=40) as r:
                    data = json.loads(r.read())
                seq = data.get("seq", seq)
                text = data.get("text")
                if text and self.enabled.is_set():
                    if text in self._ignore:
                        self._ignore.remove(text)
                    else:
                        self.on_device_text(text)
            except Exception as e:
                log.debug("clipboard poll: %s", e)
                time.sleep(2)
