"""iPhone Mirror for Windows: AirPlay screen-mirroring receiver in an iPhone-shaped window."""
import argparse
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from . import net
from .airplay import Receiver
from .config import APP_DIR, LOG_FILE, SETTINGS_FILE, Settings
from .control import OFF, Controller
from .ui import Bridge, PhoneWindow

INSTANCE_KEY = "iPhoneMirror-single-instance"
LOCAL_PORT = 7100  # --local self-tests must not collide with a normal instance on port 7000
LOCAL_SETTINGS = APP_DIR / "settings-local.json"  # ... nor overwrite its settings


def setup_logging():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
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
    logging.getLogger("zeroconf").setLevel(logging.WARNING)


def parse_args():
    p = argparse.ArgumentParser(prog="iphone_mirror")
    p.add_argument(
        "--local",
        action="store_true",
        help=f"self-test: listen on 127.0.0.1:{LOCAL_PORT} only, no mDNS, no single-instance check, "
        f"separate settings file",
    )
    p.add_argument("--port", type=int, help=f"RTSP port (default {net.PORT}, or {LOCAL_PORT} with --local)")
    p.add_argument("--control", action="store_true", help="start iPhone control (USB / WebDriverAgent) at launch")
    p.add_argument("--grab", metavar="DIR", help="self-test: save a PNG of the window every 2 s")
    p.add_argument("--quit-after", type=float, default=0, help="self-test: exit after N seconds")
    return p.parse_args()


def main():
    args = parse_args()
    if args.local:
        net.BIND_HOST = "127.0.0.1"
        net.ADVERTISE = False
        net.PORT = LOCAL_PORT
    if args.port:
        net.PORT = args.port
    setup_logging()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("iPhone Mirror")

    instance = None
    if not args.local:
        # one instance only: a second start just brings the window to the front
        probe = QLocalSocket()
        probe.connectToServer(INSTANCE_KEY)
        if probe.waitForConnected(300):
            probe.write(b"show")
            probe.waitForBytesWritten(300)
            return 0
        QLocalServer.removeServer(INSTANCE_KEY)
        instance = QLocalServer()
        instance.listen(INSTANCE_KEY)

    settings = Settings(LOCAL_SETTINGS if args.local else SETTINGS_FILE)
    logging.info(
        "starting, name=%r, port=%d, local=%s, pid=%d", settings["name"], net.PORT, args.local, os.getpid()
    )
    bridge = Bridge()
    # Controller.on_state runs on the control thread: hand it to the GUI thread via a queued signal
    ctrl = Controller(on_state=lambda state, message: bridge.control.emit(state, message))
    win = PhoneWindow(settings, None, ctrl)
    rx = Receiver(
        settings,
        on_frame=lambda arr, w, h: bridge.frame.emit(arr, w, h),
        on_event=lambda e, info: bridge.event.emit(e, info),
    )
    win.rx = rx
    # unknown iPhones must be allowed in a dialog first (setting "require_approval")
    rx.on_approve = lambda info, reply: bridge.approve.emit(info, reply)
    bridge.approve.connect(win.on_approve)
    if args.local:
        rx.muted = True  # self-test: decode audio but keep the speakers silent
    bridge.frame.connect(win.on_frame)
    bridge.event.connect(win.on_event)
    bridge.control.connect(win.on_control)
    if instance is not None:
        instance.newConnection.connect(lambda: (instance.nextPendingConnection(), win._show_front()))
    win.show()
    win._update_target()
    rx.start()
    if args.control:
        ctrl.start()
    if args.grab:
        grab_dir = Path(args.grab)
        grab_dir.mkdir(parents=True, exist_ok=True)
        counter = [0]

        def grab():
            counter[0] += 1
            win.grab().save(str(grab_dir / f"window_{counter[0]:02d}.png"))

        timer = QTimer(win, interval=2000, timeout=grab)
        timer.start()
    if args.quit_after:
        QTimer.singleShot(int(args.quit_after * 1000), app.quit)
    code = app.exec()
    settings.save()
    try:
        bridge.control.disconnect(win.on_control)  # the window is going away
    except (RuntimeError, TypeError):
        pass
    if ctrl.state != OFF:
        ctrl.stop()
    rx.stop()
    logging.info("exit")
    return code


if __name__ == "__main__":
    sys.exit(main())
