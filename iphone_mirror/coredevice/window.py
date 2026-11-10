"""The iPhone-shaped window in CoreDevice mode: live touch, keyboard and pasteboard.

PhoneWindow (ui.py) draws the window, the top bar, the frame and the touch feedback;
this subclass changes what the mouse and keyboard do over the phone:

  click / drag     touch that follows the pointer (press = finger down, release = up)
  double click     two quick touches (a real double tap)
  hold             long press (the finger simply stays down)
  wheel            scroll (a short drag that stops before lifting, so it does not fling)
  typing           keys of the iPhone's keyboard layout; other text (Vietnamese from
                   an IME, emoji...) is pasted
  Ctrl+V           paste the PC clipboard;  Ctrl+C/X/A/Z  -> Command+C/X/A/Z
  right click      menu;  Alt+drag or top bar drag  moves the window
"""
import logging
import os

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QLinearGradient, QRadialGradient
from PySide6.QtWidgets import QApplication, QMenu

from ..ui import PhoneWindow, is_key, to_fraction, VK_H, VK_J, VK_M, VK_OEM_MINUS, VK_OEM_PLUS, VK_ADD, VK_SUBTRACT
from .control import HID_LEFT_GUI, VK_TO_HID

log = logging.getLogger(__name__)

WHEEL_BATCH_MS = 90
SCROLL_PER_NOTCH = 0.12  # fraction of the screen height per wheel notch
SCROLL_MAX = 0.45
VK_PACKET = 0xE7  # Unicode text injected by an IME such as Unikey
# Ctrl+letter shortcuts that go to the iPhone as Command+letter
CMD_LETTERS = {0x41: 0x04, 0x43: 0x06, 0x58: 0x1B, 0x5A: 0x1D, 0x46: 0x09}  # A C X Z F


class LiveWindow(PhoneWindow):
    def __init__(self, settings, receiver_ref, controller):
        super().__init__(settings, receiver_ref, controller)
        self.setWindowTitle("iPhone Mirror Plus")
        self._live = None  # QPointF of the touch in progress
        self._held_keys = {}  # Windows VK -> HID usage sent on press
        self._move_flush = QTimer(self, singleShot=True, interval=12, timeout=self._flush_move)
        self._wheel_n = 0.0
        self._wheel_at = (0.5, 0.5)
        self._wheel_tm = QTimer(self, singleShot=True, interval=WHEEL_BATCH_MS, timeout=self._flush_scroll)
        self.on_restart = None  # called after a setting that needs a server restart changed
        self.clipboard_sync = None
        self._screen_off = False  # the iPhone screen is off: a click turns it on
        self.gps = None  # GpsPanel, created on first use
        self._view_rot = int(settings.get("rotation") or 0) % 360  # 0/90/180/270 clockwise
        if controller is not None:
            controller.rotation = self._view_rot
        if self._view_rot in (90, 270) and self.aspect:
            self.aspect = 1.0 / self.aspect  # start landscape if that is the saved rotation
            self._relayout(keep_center=False)

    def set_app_icon(self, icon):
        """The app's own icon for the window, the taskbar and the tray (not the AirPlay one)."""
        self.setWindowIcon(icon)
        if self._tray is not None:
            self._tray.setIcon(icon)
            self._tray.setToolTip("iPhone Mirror Plus")

    # ------------------------------------------------------------ top bar: no control button
    def _buttons(self):
        b = self._bar_rect()
        names = ("home", "pin", "min", "close")
        s = min(b.height(), max(18.0, (b.width() - 40) / len(names)))
        y = b.center().y() - s / 2
        right = b.right() - max(0.0, (b.height() - s) / 2)
        return {n: QRectF(right - (len(names) - i) * s, y, s, s) for i, n in enumerate(names)}

    def _control_status(self):
        return "" if self.control_state == "ready" else super()._control_status()

    def _button_tip(self, name):
        return {"home": "Màn hình chính (Ctrl+H)", "pin": "Luôn ở trên", "min": "Thu nhỏ", "close": "Đóng"}.get(
            name, super()._button_tip(name))

    # ------------------------------------------------------------ events from the receiver
    def on_event(self, event, info):
        if event == "starting_server":
            self.state = "starting"
            self.update()
            return
        if event == "searching":
            self.state = "searching"
            self.image = None
            self._array = None
            self.info = dict(self.info, message=info.get("message", ""))
            self.update()
            return
        if event == "lost":
            self._end_touch()
            self._release_keys()
            return
        if event in ("screen_off", "screen_on"):
            self._screen_off = event == "screen_off"
            if self._screen_off:
                self._end_touch()
            self.update()
            return
        if event == "user_paused":
            self._end_touch()
            self._release_keys()
            self.state = "user_paused"
            self.image = None
            self._array = None
            self.update()
            return
        if self.state == "user_paused" and event in ("mirror_stopped", "searching"):
            return  # stay paused until the user resumes
        if event == "connecting" and info.get("link"):
            self._hint(f"Đã kết nối qua {info['link']}")
        super().on_event(event, info)

    def _paint_placeholder(self, p, rect, r):
        g = QLinearGradient(rect.topLeft(), rect.bottomRight())
        g.setColorAt(0.0, QColor("#1d2b3f"))
        g.setColorAt(0.55, QColor("#13161f"))
        g.setColorAt(1.0, QColor("#0b0b10"))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawRoundedRect(rect, r, r)
        glow = QRadialGradient(rect.center() + QPointF(0, -rect.height() * 0.12), rect.width() * 0.6)
        glow.setColorAt(0, QColor(90, 160, 255, 70))
        glow.setColorAt(1, QColor(90, 160, 255, 0))
        p.setBrush(glow)
        p.drawRoundedRect(rect, r, r)
        iw, ih = rect.width() * 0.32, rect.width() * 0.09
        p.setBrush(QColor("#000000"))
        p.drawRoundedRect(QRectF(rect.center().x() - iw / 2, rect.y() + rect.width() * 0.035, iw, ih), ih / 2, ih / 2)
        if self.state == "user_paused":
            title = "Đã tạm dừng"
            sub = "Camera và micro trên iPhone dùng được bình thường.\n\nClick vào đây để chiếu tiếp (Ctrl+P)."
        elif self.state == "error":
            title, sub = "Chưa kết nối được", self.info.get("message", "") + "\n\nChuột phải > Kết nối lại"
        elif self.state == "connecting":
            title, sub = "Đang nhận hình...", "Mở khóa iPhone nếu màn hình đang tắt"
        elif self.state == "waiting":
            title, sub = "Đang kết nối lại...", "Giữ iPhone mở khóa, cắm cáp hoặc cùng mạng Wi-Fi"
        elif self.state == "searching":
            title = "Đang tìm iPhone..."
            sub = self.info.get("message") or "Qua cáp USB hoặc Wi-Fi. Giữ iPhone mở khóa."
        else:
            title, sub = "Đang kết nối iPhone...", "Lần đầu có thể mất khoảng một phút"
        self._center_text(p, rect, title, sub)

    # ------------------------------------------------------------ live touch
    def _frac(self, pos):
        return to_fraction(self.phone_rect(), pos)

    def _touch_ok(self, e):
        return (
            self._gestures_on()
            and not (e.modifiers() & Qt.AltModifier)
            and self.phone_rect().contains(e.position())
        )

    def mousePressEvent(self, e):
        on_phone = self.phone_rect().contains(e.position()) and not (self.hover and self._button_at(e.position()))
        if e.button() == Qt.LeftButton and self.state == "user_paused" and on_phone:
            self._toggle_pause()
            return
        if e.button() == Qt.LeftButton and self._screen_off and on_phone and self.rx is not None:
            self._wake()
            return
        if e.button() == Qt.LeftButton and on_phone and self._touch_ok(e):
            self._touch_begin(e.position())
            return
        super().mousePressEvent(e)

    def open_gps(self):
        if self.gps is None:
            from .gps import GpsPanel

            self.gps = GpsPanel(self.settings, lambda: self.rx.server if self.rx else None)
        self.gps.show()
        self.gps.raise_()
        self.gps.activateWindow()

    def _wake(self):
        import threading

        self._hint("Đang bật màn hình iPhone...")
        threading.Thread(target=self.rx.wake, name="wake", daemon=True).start()

    def rotate_view(self, step):
        """Turn the displayed window 90 degrees (step = +90 clockwise, -90 counter-clockwise).
        The iPhone always sends a portrait picture; this rotates the view and the touch mapping,
        so a sideways landscape app (video, game, map) can be watched and used upright."""
        old = self._view_rot
        self._view_rot = (old + step) % 360
        if (old % 180) != (self._view_rot % 180) and self.aspect:
            self.aspect = 1.0 / self.aspect  # portrait <-> landscape
        if self.rx is not None:
            self.rx.rotation = self._view_rot
        if self.ctrl is not None:
            self.ctrl.rotation = self._view_rot
        self.settings["rotation"] = self._view_rot
        self.settings.save()
        self._end_touch()
        self._relayout()
        self._hint("Màn hình ngang" if self._view_rot in (90, 270) else
                   ("Màn hình dọc" if self._view_rot == 0 else "Màn hình lật ngược"))
        self.update()

    def paintEvent(self, e):
        super().paintEvent(e)
        if not self._screen_off or self.image is None:
            return
        from PySide6.QtGui import QPainter

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect, r = self.phone_rect(), self.corner_radius()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 200))
        p.drawRoundedRect(rect, r, r)
        self._center_text(p, rect, "Màn hình iPhone đang tắt",
                          "Click để bật màn hình. Sau đó vuốt lên và nhập mật mã ngay trên cửa sổ "
                          "(click các số hoặc gõ số trên bàn phím).")
        p.end()

    def _toggle_pause(self):
        if self.rx is None:
            return
        if self.state == "user_paused":
            self.state = "starting"
            self.update()
            self.rx.resume()
        else:
            self.rx.pause()
            self._hint("Đã tạm dừng: camera và micro dùng được")

    def mouseDoubleClickEvent(self, e):
        # Qt turns the second press of a double click into this event: it is a touch too
        if e.button() == Qt.LeftButton and not (self.hover and self._button_at(e.position())) and self._touch_ok(e):
            self._touch_begin(e.position())
            return
        super().mouseDoubleClickEvent(e)

    def _touch_begin(self, pos):
        self._live = QPointF(pos)
        a = self._frac(pos)
        self.ctrl.touch_down(*a)
        self._add_effect("tap", a)

    def mouseMoveEvent(self, e):
        if self._live is not None:
            self._live = QPointF(e.position())
            self.ctrl.touch_move(*self._frac(e.position()))
            self._move_flush.start()
            return
        super().mouseMoveEvent(e)

    def _flush_move(self):
        if self._live is not None:
            self.ctrl.flush_move()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self._live is not None:
            self._end_touch(e.position())
            return
        super().mouseReleaseEvent(e)

    def _end_touch(self, pos=None):
        start = self._live
        self._live = None
        self._move_flush.stop()
        if start is None:
            return
        b = self._frac(pos if pos is not None else start)
        self.ctrl.touch_up(*b)

    def focusOutEvent(self, e):
        self._end_touch()
        self._release_keys()
        super().focusOutEvent(e)

    # ------------------------------------------------------------ wheel = scroll
    def wheelEvent(self, e):
        dy = e.angleDelta().y()
        if (e.modifiers() & Qt.ControlModifier) or not dy:
            super().wheelEvent(e)
            return
        if self._gestures_on() and self.phone_rect().contains(e.position()):
            if not self._wheel_tm.isActive():
                self._wheel_at = self._frac(e.position())
                self._wheel_tm.start()
            self._wheel_n += dy / 120

    def _flush_scroll(self):
        n, self._wheel_n = self._wheel_n, 0.0
        if abs(n) < 0.05 or self._live is not None:
            return
        dy = max(-SCROLL_MAX, min(SCROLL_MAX, n * SCROLL_PER_NOTCH))  # > 0: content moves down
        x = min(max(self._wheel_at[0], 0.1), 0.9)  # away from the edges (edge swipe = back)
        y0 = min(max(self._wheel_at[1], 0.15 + max(0.0, -dy)), 0.85 - max(0.0, dy))
        self.ctrl.swipe(x, y0, x, y0 + dy, seconds=0.12, steps=8, hold_end=0.06)

    # ------------------------------------------------------------ keyboard
    def keyPressEvent(self, e):
        mods = e.modifiers()
        vk = e.nativeVirtualKey()
        ctrl_only = (mods & Qt.ControlModifier) and not (mods & Qt.AltModifier)
        if ctrl_only:
            if is_key(e, (Qt.Key_P,), (0x50,)):
                if not e.isAutoRepeat():
                    self._toggle_pause()
                return
            if is_key(e, (Qt.Key_G,), (0x47,)):
                if not e.isAutoRepeat():
                    self.open_gps()
                return
            if is_key(e, (Qt.Key_Left,)):
                self.rotate_view(-90)
                return
            if is_key(e, (Qt.Key_Right,)):
                self.rotate_view(90)
                return
            if is_key(e, (Qt.Key_V,), (0x56,)):
                if not e.isAutoRepeat():
                    text = QApplication.clipboard().text()
                    if text:
                        self.ctrl.paste_text(text)
                return
            if vk in CMD_LETTERS and not e.isAutoRepeat():
                self.ctrl.key_down(HID_LEFT_GUI)
                self.ctrl.key_down(CMD_LETTERS[vk])
                self.ctrl.key_up(CMD_LETTERS[vk])
                self.ctrl.key_up(HID_LEFT_GUI)
                return
            if is_key(e, (Qt.Key_L,), (0x4C,)):
                if not e.isAutoRepeat():
                    self.ctrl.press("lock")
                return
            if is_key(e, (Qt.Key_S,), (0x53,)):
                if not e.isAutoRepeat():
                    self.ctrl.press("siri")
                return
            super().keyPressEvent(e)  # Home, App Switcher, volume, mute, resize
            return
        if not self._control_ready():
            super().keyPressEvent(e)
            return
        if e.isAutoRepeat():
            return  # the iPhone repeats a held key by itself
        text = e.text()
        if vk == VK_PACKET or (text and any(ord(c) > 127 for c in text)):
            if text:
                self.ctrl.paste_text(text)
            return
        usage = VK_TO_HID.get(vk)
        if usage is None:
            super().keyPressEvent(e)
            return
        self._held_keys[vk] = usage
        self.ctrl.key_down(usage)

    def keyReleaseEvent(self, e):
        if e.isAutoRepeat():
            return
        usage = self._held_keys.pop(e.nativeVirtualKey(), None)
        if usage is not None:
            self.ctrl.key_up(usage)
            return
        super().keyReleaseEvent(e)

    def _release_keys(self):
        self._held_keys.clear()
        if self.ctrl is not None:
            self.ctrl.release_keys()

    # ------------------------------------------------------------ menu
    def contextMenuEvent(self, e):
        self._end_touch()
        m = QMenu(self)
        ready = self._control_ready()
        if self.state == "user_paused":
            m.addAction("Tiếp tục chiếu màn hình\tCtrl+P", self._toggle_pause)
        else:
            m.addAction("Tạm dừng (để dùng camera, micro)\tCtrl+P", self._toggle_pause)
        m.addSeparator()
        for label, fn in (
            ("Màn hình chính\tCtrl+H", lambda: self.ctrl.home()),
            ("Đa nhiệm\tCtrl+J", lambda: self.ctrl.app_switcher()),
            ("Khóa màn hình\tCtrl+L", lambda: self.ctrl.press("lock")),
            ("Siri\tCtrl+S", lambda: self.ctrl.press("siri")),
            ("Tăng âm lượng\tCtrl+Up", lambda: self.ctrl.press("volume-up")),
            ("Giảm âm lượng\tCtrl+Down", lambda: self.ctrl.press("volume-down")),
        ):
            a = m.addAction(label, fn)
            a.setEnabled(ready)
        paste = m.addAction("Dán từ máy tính\tCtrl+V",
                            lambda: self.ctrl.paste_text(QApplication.clipboard().text()))
        paste.setEnabled(ready and bool(QApplication.clipboard().text()))
        m.addSeparator()
        sync = QAction("Đồng bộ clipboard hai chiều", m, checkable=True,
                       checked=bool(self.settings.get("clipboard_sync", True)))
        sync.triggered.connect(self._toggle_clipboard_sync)
        m.addAction(sync)
        rot = m.addMenu("Xoay màn hình")
        rot.addAction("Xoay sang ngang (trái)\tCtrl+←", lambda: self.rotate_view(-90))
        rot.addAction("Xoay sang ngang (phải)\tCtrl+→", lambda: self.rotate_view(90))
        rot.addAction("Về màn hình dọc", lambda: self.rotate_view((360 - self._view_rot) % 360))
        snd = QAction("Âm thanh\tCtrl+M", m, checkable=True, checked=not (self.rx and self.rx.muted))
        snd.triggered.connect(self._toggle_mute)
        m.addAction(snd)
        ltrp = QAction("Hình nét hơn khi chuyển động (LTRP)", m, checkable=True,
                       checked=bool(self.settings.get("ltrp", True)))
        ltrp.triggered.connect(self._toggle_ltrp)
        m.addAction(ltrp)
        top = QAction("Luôn ở trên", m, checkable=True, checked=self.settings.get("always_on_top", True))
        top.triggered.connect(lambda: self._button("pin"))
        m.addAction(top)
        size = m.addMenu("Kích thước")
        screen = (self.screen() or QApplication.primaryScreen()).availableGeometry()
        for label, k in (("Nhỏ", 0.5), ("Vừa", 0.72), ("Lớn", 0.92)):
            size.addAction(label, lambda k=k: self.set_phone_height(screen.height() * k))
        m.addSeparator()
        m.addAction("Giả lập GPS / lộ trình...\tCtrl+G", self.open_gps)
        m.addAction("Chụp màn hình", self.screenshot)
        m.addAction("Kết nối lại", self._reconnect)
        m.addAction("Bật dùng qua Wi-Fi (cần cắm cáp 1 lần)", self._enable_wifi)
        m.addAction("Mở log", self._open_logs)
        m.addSeparator()
        m.addAction("Thoát", QApplication.quit)
        m.exec(e.globalPos())

    def _toggle_clipboard_sync(self):
        on = not self.settings.get("clipboard_sync", True)
        self.settings["clipboard_sync"] = on
        self.settings.save()
        if self.clipboard_sync is not None:
            (self.clipboard_sync.enabled.set if on else self.clipboard_sync.enabled.clear)()
        self._hint("Đồng bộ clipboard: bật" if on else "Đồng bộ clipboard: tắt")

    def _toggle_ltrp(self):
        on = not self.settings.get("ltrp", True)
        self.settings["ltrp"] = on
        self.settings.save()
        self._hint("Đang kết nối lại...")
        self._reconnect()

    def _reconnect(self):
        if self.on_restart:
            self.on_restart()

    def _enable_wifi(self):
        import threading

        from PySide6.QtWidgets import QMessageBox

        from .server import enable_wifi

        self._hint("Đang bật kết nối Wi-Fi...")
        result = {}

        def work():
            result["r"] = enable_wifi()

        def done():
            if t.is_alive():
                QTimer.singleShot(300, done)
                return
            ok, msg = result.get("r", (False, "?"))
            box = QMessageBox(QMessageBox.Information if ok else QMessageBox.Warning, "Wi-Fi", msg,
                              QMessageBox.Ok, self)
            box.setWindowModality(Qt.NonModal)
            box.setAttribute(Qt.WA_DeleteOnClose)
            box.show()

        t = threading.Thread(target=work, daemon=True)
        t.start()
        QTimer.singleShot(300, done)

    def _open_logs(self):
        from ..config import APP_DIR

        os.startfile(str(APP_DIR))
