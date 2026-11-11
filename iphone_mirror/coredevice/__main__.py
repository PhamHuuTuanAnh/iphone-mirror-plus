"""iPhone Mirror Plus: the iPhone (iOS 27+) in a window with live touch, sound and a
shared clipboard, over Apple's CoreDevice display service.

    python -m iphone_mirror.coredevice [--grab DIR] [--quit-after N]
"""
import argparse
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from ..config import APP_DIR, Settings
from ..ui import Bridge
from .control import ClipboardSync, LiveControl
from .stream import CoreDeviceReceiver
from .window import LiveWindow

INSTANCE_KEY = "iPhoneMirrorPlus-single-instance"
# Windows groups taskbar buttons, pins and notifications by this ID; without it the window
# shows up as "Python". The Start menu / Desktop shortcuts carry the same ID.
APP_ID = "iPhoneMirrorPlus.App"
ICON_FILE = Path(__file__).with_name("app.ico")


def set_app_identity():
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:
            logging.exception("could not set the AppUserModelID")
SETTINGS_FILE = APP_DIR / "settings-plus.json"
LOG_FILE = APP_DIR / "iphone_mirror_plus.log"


class _Signals(QObject):
    device_text = Signal(str)  # text copied on the iPhone (clipboard sync)
    ready = Signal(object)  # the server is up
    failed = Signal(str)


def setup_logging():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    fh = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    fh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)
    if sys.stderr:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


def main():
    p = argparse.ArgumentParser(prog="iphone_mirror.coredevice")
    p.add_argument("--grab", metavar="DIR", help="self-test: save a PNG of the window every 2 s")
    p.add_argument("--quit-after", type=float, default=0, help="self-test: exit after N seconds")
    args = p.parse_args()
    setup_logging()
    from .perf import boost_process

    boost_process()
    set_app_identity()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("iPhone Mirror Plus")
    app.setApplicationDisplayName("iPhone Mirror Plus")
    icon = QIcon(str(ICON_FILE))
    app.setWindowIcon(icon)

    probe = QLocalSocket()
    probe.connectToServer(INSTANCE_KEY)
    if probe.waitForConnected(300):
        probe.write(b"show")
        probe.waitForBytesWritten(300)
        return 0
    QLocalServer.removeServer(INSTANCE_KEY)
    instance = QLocalServer()
    instance.listen(INSTANCE_KEY)

    settings = Settings(SETTINGS_FILE)
    for k, v in (("ltrp", True), ("clipboard_sync", True), ("audio", True)):
        if k not in settings.data:
            settings[k] = v
    logging.info("starting iPhone Mirror Plus, pid=%d", os.getpid())

    bridge = Bridge()
    sig = _Signals()
    ctrl = LiveControl(on_state=lambda state, message: bridge.control.emit(state, message))
    win = LiveWindow(settings, None, ctrl)
    win.set_app_icon(icon)
    rx = CoreDeviceReceiver(
        settings,
        on_frame=lambda arr, w, h: bridge.frame.emit(arr, w, h),
        on_event=lambda e, info: bridge.event.emit(e, info),
        on_ready=lambda server: sig.ready.emit(server),
    )
    win.rx = rx
    rx.rotation = win._view_rot  # restore the saved landscape/portrait orientation
    bridge.frame.connect(win.on_frame)
    bridge.event.connect(win.on_event)
    bridge.control.connect(win.on_control)

    # clipboard: iPhone -> PC through the long poll, PC -> iPhone when the PC clipboard changes
    clip = QApplication.clipboard()
    last_from_device = [None]

    def device_text(text):
        last_from_device[0] = text
        clip.setText(text)

    def pc_changed():
        text = clip.text()
        if text and text != last_from_device[0] and ctrl.server is not None and settings.get("clipboard_sync", True):
            if sync is not None:
                sync.ignore_next_device_text(text)
            ctrl.set_clipboard(text)

    sig.device_text.connect(device_text)
    sync = ClipboardSync(ctrl, on_device_text=lambda t: sig.device_text.emit(t))
    ctrl.clipboard_sync = sync
    win.clipboard_sync = sync
    if settings.get("clipboard_sync", True):
        sync.enabled.set()
    sync.start()
    clip.dataChanged.connect(pc_changed)

    def on_ready(server):
        ctrl.attach(server)
        if win.gps is not None:
            win.gps.drive.resend()  # a new connection starts on the real GPS again

    def on_event(event, info):
        if event in ("error", "lost", "searching", "user_paused"):
            ctrl.detach()

    sig.ready.connect(on_ready)
    bridge.event.connect(on_event)

    def restart():
        ctrl.detach()
        rx.restart()

    win.on_restart = restart
    instance.newConnection.connect(lambda: (instance.nextPendingConnection(), win._show_front()))

    # start a bit taller than the AirPlay window: the CoreDevice picture is the full panel
    if not settings.get("phone_height"):
        screen = app.primaryScreen().availableGeometry()
        win.set_phone_height(int(screen.height() * 0.86))
    win.show()
    win._update_target()
    rx.start()

    if args.grab:
        from pathlib import Path

        grab_dir = Path(args.grab)
        grab_dir.mkdir(parents=True, exist_ok=True)
        counter = [0]

        def grab():
            counter[0] += 1
            win.grab().save(str(grab_dir / f"window_{counter[0]:02d}.png"))

        QTimer(win, interval=2000, timeout=grab).start()
    if args.quit_after:
        QTimer.singleShot(int(args.quit_after * 1000), app.quit)

    code = app.exec()
    settings.save()
    sync.stop()
    ctrl.close()
    rx.stop()
    logging.info("exit")
    return code


if __name__ == "__main__":
    sys.exit(main())
