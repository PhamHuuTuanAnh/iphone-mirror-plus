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
import math
import os
import time
import unicodedata

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (QAction, QColor, QFont, QFontMetrics, QLinearGradient, QPainterPath, QPen, QPolygonF,
                           QRadialGradient)
from PySide6.QtWidgets import QApplication, QMenu

from ..ui import (BAR, SHADOW, PhoneWindow, is_key, to_fraction, VK_H, VK_J, VK_M, VK_OEM_MINUS, VK_OEM_PLUS,
                  VK_ADD, VK_SUBTRACT)
from .control import HID_BACKSPACE, HID_LEFT_GUI, MODIFIER_USAGES, VK_TO_HID

log = logging.getLogger(__name__)

# top bar
LIGHT_D, LIGHT_GAP = 12.0, 8.0  # traffic light diameter and gap
BAR_ICONS = ("sound", "rotate", "home", "switcher", "reconnect", "settings")
DROP_ORDER = ("reconnect", "rotate", "sound", "switcher", "home")  # dropped first on a narrow window

WHEEL_BATCH_MS = 90
SCROLL_PER_NOTCH = 0.12  # fraction of the screen height per wheel notch
SCROLL_MAX = 0.45
VK_PACKET = 0xE7  # Unicode text injected by an IME such as Unikey
TEXT_BATCH_MS = 8  # input-method Backspaces and Unicode packets this close together are one edit
COMPOSE_IDLE_MS = 300  # a word with input-method edits is sent after this pause at the latest
VK_BACK, VK_CAPITAL = 0x08, 0x14
# Ctrl+letter shortcuts that go to the iPhone as Command+letter
CMD_LETTERS = {0x41: 0x04, 0x43: 0x06, 0x58: 0x1B, 0x5A: 0x1D, 0x46: 0x09}  # A C X Z F



def clean_text(text):
    """Join UTF-16 surrogate halves that came as separate events, drop control characters."""
    try:
        text = text.encode("utf-16-le", "surrogatepass").decode("utf-16-le")
    except UnicodeError:
        text = "".join(c for c in text if not 0xD800 <= ord(c) <= 0xDFFF)
    return "".join(c for c in text if c.isprintable() or c in "\n\t")


class LiveWindow(PhoneWindow):
    def __init__(self, settings, receiver_ref, controller):
        super().__init__(settings, receiver_ref, controller)
        self.setWindowTitle("iPhone Mirror Plus")
        self.setAttribute(Qt.WA_InputMethodEnabled, True)  # see inputMethodEvent
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
        self._link = ""  # "USB" / "Wi-Fi" of the current connection (top bar)
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

    # ------------------------------------------------------------ top bar (macOS-style pill)
    # [● ● ●  ● iPhone (USB)        🔊  ⟳  ⌂  ❐  ↻  ⚙]
    def _bar_rect(self) -> QRectF:
        rect = self.phone_rect()
        grow = min(12.0, SHADOW - 4)  # a little wider than the phone, into the shadow margin
        return QRectF(rect.x() - grow, SHADOW + 1, rect.width() + 2 * grow, BAR - 9)

    def _bar_layout(self):
        """(buttons, label rect). Traffic lights left, icons right; on a narrow window the
        least used icons are dropped first, then the label is shortened."""
        b = self._bar_rect()
        cy = b.center().y()
        d = LIGHT_D
        x = b.x() + 13
        btn = {}
        for n in ("close", "min", "zoom"):
            btn[n] = QRectF(x - 4, cy - d / 2 - 4, d + 8, d + 8)
            x += d + LIGHT_GAP
        left = x - LIGHT_GAP + 12
        s = min(b.height() - 5, 25.0)
        icons = list(BAR_ICONS)
        while icons and len(icons) * s + 8 > b.right() - left - 40:
            icons.remove(next(n for n in DROP_ORDER if n in icons))
        right = b.right() - 6
        for i, n in enumerate(icons):
            btn[n] = QRectF(right - (len(icons) - i) * s, cy - s / 2, s, s)
        first = right - len(icons) * s
        return btn, QRectF(left, b.y(), max(0.0, first - left - 6), b.height())

    def _buttons(self):
        return self._bar_layout()[0]

    def _control_status(self):
        return "" if self.control_state == "ready" else super()._control_status()

    def _status_color(self):
        if self.state == "error" or self.control_state == "error":
            return QColor("#ff453a")
        if self.state == "user_paused" or self._screen_off:
            return QColor("#8e8e93")
        if self.state == "mirroring" and self.control_state == "ready":
            return QColor("#30d158")
        return QColor("#ff9f0a")

    def _bar_labels(self):
        if self._hint_text and time.monotonic() < self._hint_until:
            return [self._hint_text]
        if self.state == "user_paused":
            return ["Đã tạm dừng", "Tạm dừng"]
        name = f"iPhone ({self._link})" if self._link else "iPhone"
        if self.state != "mirroring":
            return [f"{name} · đang kết nối", name]
        status = self._control_status()
        return [f"{name} · {status}", name] if status else [name]

    def _fps_text(self):
        """Frame rate shown after the name in a smaller, dimmer font (not over a hint)."""
        if self.state != "mirroring" or not self.fps:
            return ""
        if self._hint_text and time.monotonic() < self._hint_until:
            return ""
        return f"{self.fps:.0f} fps"

    def _paint_bar(self, p):
        b = self._bar_rect()
        btn, text_rect = self._bar_layout()
        rad = b.height() / 2
        p.save()
        p.setPen(Qt.NoPen)
        for i in (3, 2, 1):  # soft shadow under the pill
            p.setBrush(QColor(0, 0, 0, 22))
            p.drawRoundedRect(b.adjusted(-i, -i + 2, i, i + 2), rad + i, rad + i)
        g = QLinearGradient(b.topLeft(), b.bottomLeft())
        g.setColorAt(0, QColor(44, 44, 48, 240))
        g.setColorAt(1, QColor(26, 26, 30, 240))
        p.setBrush(g)
        p.setPen(QPen(QColor(255, 255, 255, 26), 1))
        p.drawRoundedRect(b.adjusted(0.5, 0.5, -0.5, -0.5), rad, rad)

        # traffic lights; their glyphs show while the mouse is over any of them
        over_lights = self._hover_btn in ("close", "min", "zoom")
        for n, col in (("close", "#ff5f57"), ("min", "#febc2e"), ("zoom", "#28c840")):
            c = btn[n].center()
            p.setPen(QPen(QColor(0, 0, 0, 60), 0.8))
            p.setBrush(QColor(col))
            p.drawEllipse(c, LIGHT_D / 2, LIGHT_D / 2)
            if over_lights:
                self._paint_light_glyph(p, n, c)

        # status dot + device name
        font = QFont("Segoe UI Semibold", 9)
        fm = QFontMetrics(font)
        dot = self._status_color()
        tx = text_rect.x() + 13
        if text_rect.width() > 24:
            p.setPen(Qt.NoPen)
            glow = QColor(dot)
            glow.setAlpha(60)
            p.setBrush(glow)
            p.drawEllipse(QPointF(text_rect.x() + 4, b.center().y()), 5, 5)
            p.setBrush(dot)
            p.drawEllipse(QPointF(text_rect.x() + 4, b.center().y()), 3.4, 3.4)
            room = QRectF(tx, b.y(), max(0.0, text_rect.right() - tx), b.height())
            labels = self._bar_labels()
            label = next((t for t in labels if fm.horizontalAdvance(t) <= room.width()), None)
            if label is None:
                label = fm.elidedText(labels[-1], Qt.ElideRight, int(room.width()))
            p.setFont(font)
            p.setPen(QColor(255, 255, 255, 235))
            p.drawText(room, Qt.AlignVCenter | Qt.AlignLeft, label)
            fps = self._fps_text()
            if fps:
                small = QFont("Segoe UI", 8)
                fx = room.x() + fm.horizontalAdvance(label) + 7
                if fx + QFontMetrics(small).horizontalAdvance(fps) <= room.right():
                    p.setFont(small)
                    p.setPen(QColor(255, 255, 255, 140))
                    p.drawText(QRectF(fx, b.y() + 0.5, room.right() - fx, b.height()),
                               Qt.AlignVCenter | Qt.AlignLeft, fps)

        # icons
        ready = self._control_ready()
        for n in BAR_ICONS:
            if n not in btn:
                continue
            r = btn[n]
            c = r.center()
            if self._hover_btn == n:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(255, 255, 255, 34))
                p.drawRoundedRect(r.adjusted(2, 2, -2, -2), 7, 7)
            k = r.width() / 28
            col = QColor(255, 255, 255, 225)
            if n in ("home", "switcher") and not ready:
                col = QColor(255, 255, 255, 95)
            if n == "sound":
                muted = bool(self.rx and self.rx.muted)
                col = QColor(255, 255, 255, 150) if muted else QColor("#3d9bff")
                self._glyph_sound(p, c, k, col, muted)
            else:
                getattr(self, f"_glyph_{n}")(p, c, k, col)
        p.restore()

    # --- glyphs (drawn around c on a 30 px grid scaled by k)
    @staticmethod
    def _pen(p, col, k, w=1.6):
        p.setPen(QPen(col, w * max(1.0, k), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)

    @staticmethod
    def _paint_light_glyph(p, name, c):
        col = QColor(0, 0, 0, 150)
        p.setPen(QPen(col, 1.3, Qt.SolidLine, Qt.RoundCap))
        p.setBrush(Qt.NoBrush)
        e = 2.6
        if name == "close":
            p.drawLine(QPointF(c.x() - e, c.y() - e), QPointF(c.x() + e, c.y() + e))
            p.drawLine(QPointF(c.x() - e, c.y() + e), QPointF(c.x() + e, c.y() - e))
        elif name == "min":
            p.drawLine(QPointF(c.x() - 3.2, c.y()), QPointF(c.x() + 3.2, c.y()))
        else:  # zoom: two small corner triangles
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            p.drawPolygon(QPolygonF([QPointF(c.x() - 3.3, c.y() - 3.3), QPointF(c.x() + 0.7, c.y() - 3.3),
                                     QPointF(c.x() - 3.3, c.y() + 0.7)]))
            p.drawPolygon(QPolygonF([QPointF(c.x() + 3.3, c.y() + 3.3), QPointF(c.x() - 0.7, c.y() + 3.3),
                                     QPointF(c.x() + 3.3, c.y() - 0.7)]))

    def _glyph_sound(self, p, c, k, col, muted):
        def pt(x, y):
            return QPointF(c.x() + x * k, c.y() + y * k)

        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawPolygon(QPolygonF([pt(-7, -2.6), pt(-4, -2.6), pt(0, -6.4), pt(0, 6.4), pt(-4, 2.6), pt(-7, 2.6)]))
        self._pen(p, col, k)
        if muted:
            p.drawLine(pt(3, -3), pt(8, 3))
            p.drawLine(pt(3, 3), pt(8, -3))
        else:
            for rr in (3.6, 7.0):
                p.drawArc(QRectF(c.x() + (0.5 - rr) * k, c.y() - rr * k, 2 * rr * k, 2 * rr * k), -50 * 16, 100 * 16)

    def _arc_arrow(self, p, c, k, col, radius, start, span, head=3.0):
        """Arc (Qt degrees, counter-clockwise from 3 o'clock) with an arrow head at its end."""
        self._pen(p, col, k)
        r = radius * k
        p.drawArc(QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r), int(start * 16), int(span * 16))
        a = math.radians(start + span)
        end = QPointF(c.x() + r * math.cos(a), c.y() - r * math.sin(a))
        sgn = 1 if span > 0 else -1
        tx, ty = -math.sin(a) * sgn, -math.cos(a) * sgn  # direction of travel (y down)
        nx, ny = -ty, tx
        h = head * k
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawPolygon(QPolygonF([
            QPointF(end.x() + tx * h * 1.1, end.y() + ty * h * 1.1),
            QPointF(end.x() + nx * h - tx * h * 0.5, end.y() + ny * h - ty * h * 0.5),
            QPointF(end.x() - nx * h - tx * h * 0.5, end.y() - ny * h - ty * h * 0.5),
        ]))

    def _glyph_rotate(self, p, c, k, col):
        # a phone outline with an arrow turning around its top corner
        self._pen(p, col, k)
        p.drawRoundedRect(QRectF(c.x() - 6.5 * k, c.y() - 2.5 * k, 8.5 * k, 9.5 * k), 1.8 * k, 1.8 * k)
        self._arc_arrow(p, QPointF(c.x() - 1 * k, c.y() + 1 * k), k, col, 7.2, 110, -95, head=2.6)

    def _glyph_home(self, p, c, k, col):
        self._pen(p, col, k)
        path = QPainterPath(QPointF(c.x() - 7 * k, c.y() - 0.2 * k))
        path.lineTo(c.x(), c.y() - 6.8 * k)
        path.lineTo(c.x() + 7 * k, c.y() - 0.2 * k)
        path.moveTo(c.x() - 5 * k, c.y() - 1.8 * k)
        path.lineTo(c.x() - 5 * k, c.y() + 6.2 * k)
        path.lineTo(c.x() - 1.6 * k, c.y() + 6.2 * k)
        path.lineTo(c.x() - 1.6 * k, c.y() + 2.4 * k)
        path.lineTo(c.x() + 1.6 * k, c.y() + 2.4 * k)
        path.lineTo(c.x() + 1.6 * k, c.y() + 6.2 * k)
        path.lineTo(c.x() + 5 * k, c.y() + 6.2 * k)
        path.lineTo(c.x() + 5 * k, c.y() - 1.8 * k)
        p.drawPath(path)

    def _glyph_switcher(self, p, c, k, col):
        self._pen(p, col, k)
        p.drawRoundedRect(QRectF(c.x() - 2.2 * k, c.y() - 6.6 * k, 9 * k, 9 * k), 2 * k, 2 * k)
        p.setBrush(QColor(34, 34, 38))
        p.drawRoundedRect(QRectF(c.x() - 6.8 * k, c.y() - 2.4 * k, 9 * k, 9 * k), 2 * k, 2 * k)

    def _glyph_reconnect(self, p, c, k, col):
        self._arc_arrow(p, c, k, col, 6.4, 120, -290, head=2.8)

    def _glyph_settings(self, p, c, k, col):
        pts = []
        for t in range(8):
            for rr, da in ((5.0, -15), (7.4, -8), (7.4, 8), (5.0, 15)):
                ang = math.radians(t * 45 + da)
                pts.append(QPointF(c.x() + rr * k * math.cos(ang), c.y() + rr * k * math.sin(ang)))
        self._pen(p, col, k, 1.4)
        p.drawPolygon(QPolygonF(pts))
        p.drawEllipse(c, 2.3 * k, 2.3 * k)

    def _button_tip(self, name):
        muted = bool(self.rx and self.rx.muted)
        return {
            "close": "Đóng",
            "min": "Thu nhỏ",
            "zoom": "Phóng to / thu về",
            "sound": ("Bật âm thanh" if muted else "Tắt âm thanh") + " (Ctrl+M)",
            "rotate": "Xoay màn hình (Ctrl+← / Ctrl+→)",
            "home": "Màn hình chính (Ctrl+H)",
            "switcher": "Đa nhiệm (Ctrl+J)",
            "reconnect": "Kết nối lại",
            "settings": "Cài đặt và chức năng khác",
        }.get(name, "")

    def _button(self, name):
        if name == "zoom":
            self._toggle_size()
        elif name == "sound":
            self._toggle_mute()
        elif name == "rotate":
            self.rotate_view(90)
        elif name == "home":
            self._phone("home")
        elif name == "switcher":
            self._phone("app_switcher")
        elif name == "reconnect":
            self._hint("Đang kết nối lại...")
            self._reconnect()
        elif name == "settings":
            r = self._buttons()["settings"]
            self._show_menu(self.mapToGlobal(r.bottomLeft().toPoint()))
        else:
            super()._button(name)
        self.update()

    def _toggle_mute(self):
        if self.rx:
            self.rx.set_muted(not self.rx.muted)
            self._hint("Đã tắt âm thanh" if self.rx.muted else "Đã bật âm thanh")

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
            self._link = info["link"]
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
        self._flush_word(reset=True)  # a touch may move the text cursor
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
        self._flush_word(reset=True)
        dy = max(-SCROLL_MAX, min(SCROLL_MAX, n * SCROLL_PER_NOTCH))  # > 0: content moves down
        x = min(max(self._wheel_at[0], 0.1), 0.9)  # away from the edges (edge swipe = back)
        y0 = min(max(self._wheel_at[1], 0.15 + max(0.0, -dy)), 0.85 - max(0.0, dy))
        self.ctrl.swipe(x, y0, x, y0 + dy, seconds=0.12, steps=8, hold_end=0.06)

    # ------------------------------------------------------------ keyboard
    # Vietnamese input methods (EVKey, Unikey...) eat the key that adds a mark and send
    # Backspaces (real VK_BACK presses) plus the new text as Unicode packets (VK_PACKET):
    # "tuwngf" -> t u, [Backspace, "ư"], n g, [3 x Backspace, "ừ", "n", "g"]. A hardware
    # keyboard cannot type "ừ", so such text is pasted, and iOS applies a paste
    # asynchronously: keys sent right after Command+V can land before the pasted text
    # (measured on an iPhone 14 Pro Max: 0-100 ms later scrambles the word, 200-300 ms is
    # reliable). Pasting on every mark would make "người" wait ~1 s, so a word is composed
    # on the PC:
    #   * keys go to the iPhone at once while the word has no input-method edit;
    #   * from the first edit on, the word is kept here and the iPhone keeps what it has;
    #   * at the end of the word (space, punctuation, Enter, any other key, a touch, or a
    #     COMPOSE_IDLE_MS pause) the iPhone copy is fixed in one go: Backspaces back to the
    #     common start, then the rest typed (plain ASCII) or pasted once.
    def _kb_state(self):
        if not hasattr(self, "_word_dev"):
            self._word_dev = ""  # the current word as the iPhone has it (what was sent)
            self._word_loc = ""  # the current word as the input method has it
            self._word_pre = 0  # input-method Backspaces that went past the start of the word
            self._ime_batch = []  # "\b" and text pieces of one input-method burst
            self._ime_timer = QTimer(self, singleShot=True, interval=TEXT_BATCH_MS, timeout=self._ime_flush_batch)
            self._idle_timer = QTimer(self, singleShot=True, interval=COMPOSE_IDLE_MS, timeout=self._flush_word)

    def _word_dirty(self):
        return self._word_dev != self._word_loc or self._word_pre > 0

    def _ime_push(self, item):
        self._kb_state()
        self._ime_batch.append(item)
        self._ime_timer.start()

    def _ime_flush_batch(self):
        """Apply one burst: Backspaces alone are the user's; with text they are an edit."""
        self._kb_state()
        batch, self._ime_batch = self._ime_batch, []
        self._ime_timer.stop()
        if not batch:
            return
        tokens = []
        for item in batch:
            if item != "\b" and tokens and tokens[-1] != "\b":
                tokens[-1] += item
            else:
                tokens.append(item)
        tokens = [t if t == "\b" else clean_text(t) for t in tokens]
        if all(t == "\b" for t in tokens):
            for _ in tokens:
                self._user_backspace()
            return
        for t in tokens:
            if t == "\b":
                if self._word_loc:
                    self._word_loc = self._word_loc[:-1]
                else:
                    self._word_pre += 1
            else:
                self._word_loc += t
        self._idle_timer.start()

    def _user_backspace(self):
        if not self._word_dirty():
            self.ctrl.tap(HID_BACKSPACE)
            if self._word_dev:
                self._word_dev = self._word_loc = self._word_dev[:-1]
            return
        if self._word_loc:
            self._word_loc = self._word_loc[:-1]
        else:
            self._word_pre += 1
        self._idle_timer.start()

    def _flush_word(self, reset=False):
        """Make the iPhone show the word as the input method has it."""
        self._kb_state()
        self._ime_flush_batch()
        self._idle_timer.stop()
        dev, loc, pre = self._word_dev, self._word_loc, self._word_pre
        if (dev != loc or pre) and self.ctrl is not None:
            keep = 0
            if not pre:
                while keep < min(len(dev), len(loc)) and dev[keep] == loc[keep]:
                    keep += 1
            n = len(dev) - keep + pre
            if n:
                self.ctrl.tap(HID_BACKSPACE, n)
            rest = loc[keep:]
            if rest:
                self.ctrl.type_unicode(rest)
            self._word_dev, self._word_pre = loc, 0
            if any(ord(c) > 0xFFFF or unicodedata.category(c) in ("Mn", "Me", "Cf", "Sk") for c in rest):
                reset = True  # iOS deletes emoji and combined marks per glyph: stop counting here
        if reset:
            self._word_dev = self._word_loc = ""
            self._word_pre = 0

    def inputMethodEvent(self, e):
        """Committed text from a Windows input method (the built-in Vietnamese/Chinese/Japanese
        IMEs) and characters outside the BMP: Qt turns an emoji sent as two UTF-16 packets
        (the Win+. panel, EVKey's emoji) into a commit string, not into key events."""
        text = clean_text(e.commitString())
        if text and self._control_ready():
            self._kb_state()
            self._ime_flush_batch()
            self._word_loc += text
            self._flush_word(reset=True)
        e.accept()

    def inputMethodQuery(self, query):
        if query == Qt.ImEnabled:
            return self._control_ready()
        if query == Qt.ImHints:
            return Qt.ImhNoPredictiveText
        if query == Qt.ImCursorRectangle:  # where an IME shows its candidate window
            r = self.phone_rect()
            return QRectF(r.center().x(), r.center().y(), 1, 20).toRect()
        return super().inputMethodQuery(query)

    def keyPressEvent(self, e):
        mods = e.modifiers()
        vk = e.nativeVirtualKey()
        ctrl_only = (mods & Qt.ControlModifier) and not (mods & Qt.AltModifier)
        text = e.text()
        ready = self._control_ready()
        if ready and not ctrl_only:
            if vk == VK_PACKET or (text and any(ord(c) > 127 for c in text)):
                if text and not e.isAutoRepeat():
                    self._ime_push(text)
                return
            if vk == VK_BACK:  # a held Backspace repeats on the PC side: one tap each
                self._ime_push("\b")
                return
        if ready:
            self._kb_state()
            self._ime_flush_batch()
        if ctrl_only:
            if ready:
                self._flush_word(reset=True)
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
        if not ready:
            super().keyPressEvent(e)
            return
        usage = VK_TO_HID.get(vk)
        word_char = len(text) == 1 and text.isalnum()
        if e.isAutoRepeat():
            if usage not in MODIFIER_USAGES:
                self._flush_word(reset=True)  # the iPhone repeats a held key itself: stop counting
            return
        if usage is None:
            super().keyPressEvent(e)
            return
        if usage in MODIFIER_USAGES or vk == VK_CAPITAL:
            pass  # Shift for a capital letter is part of the word
        elif word_char:
            if self._word_dirty():
                self._word_loc += text  # sent when the word ends
                self._idle_timer.start()
                return
            self._word_dev += text
            self._word_loc += text
        else:
            self._flush_word(reset=True)  # space, punctuation, Enter, arrows...: the word ends
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
        if self.ctrl is not None and self._control_ready():
            self._flush_word(reset=True)
        self._held_keys.clear()
        if self.ctrl is not None:
            self.ctrl.release_keys()

    # ------------------------------------------------------------ menu
    def contextMenuEvent(self, e):
        self._show_menu(e.globalPos())

    def _show_menu(self, global_pos):
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
        idr = QAction("Nét lại nhanh sau khi vuốt (giảm FPS một nửa)", m, checkable=True,
                      checked=bool(self.settings.get("motion_idr", False)))
        idr.triggered.connect(self._toggle_motion_idr)
        m.addAction(idr)
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
        m.exec(global_pos)

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

    def _toggle_motion_idr(self):
        on = not self.settings.get("motion_idr", False)
        self.settings["motion_idr"] = on
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
