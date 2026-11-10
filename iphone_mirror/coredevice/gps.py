"""GPS simulation for testing navigation / routing apps on the iPhone.

The iPhone takes simulated positions through the server child (extras.py, DVT
LocationSimulation): latitude/longitude only, kept while the channel is open. To look like
a real drive this panel sends a new point every second, moving along a route:

  * the route comes from OSRM (real roads between start, via points and destination), or
    from a GPX / KML / GeoJSON / CSV file, or is a single point;
  * the speed follows a target (km/h) but slows down before sharp turns and accelerates /
    brakes gradually, like a car;
  * optional GPS noise (meters) tests map matching; x2 / x5 replays faster; pause holds the
    position (a stopped car); loop restarts at the end.

Points can be typed as "lat, lon" or pasted as a Google Maps link.
"""
import json
import logging
import math
import random
import re
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox,
    QVBoxLayout, QWidget,
)

log = logging.getLogger(__name__)

EARTH_R = 6371008.8
OSRM_URL = "https://router.project-osrm.org/route/v1/driving/{coords}?overview=full&geometries=geojson"
TICK_S = 1.0  # one position per second, like a phone GPS
TURN_LOOKAHEAD_M = 60.0
ACCEL = 2.0  # m/s^2 speeding up
BRAKE = 3.0  # m/s^2 slowing down


# ---------------------------------------------------------------------- geometry
def haversine(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * EARTH_R * math.asin(min(1.0, math.sqrt(h)))


def bearing(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    y = math.sin(lo2 - lo1) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(lo2 - lo1)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def offset(p, north_m, east_m):
    lat = p[0] + math.degrees(north_m / EARTH_R)
    lon = p[1] + math.degrees(east_m / (EARTH_R * math.cos(math.radians(p[0]))))
    return lat, lon


class Route:
    """A polyline with cumulative distances, for position-by-distance lookups."""

    def __init__(self, points):
        pts = [tuple(p) for p in points]
        # drop consecutive duplicates
        self.points = [p for i, p in enumerate(pts) if i == 0 or haversine(pts[i - 1], p) > 0.05]
        self.cum = [0.0]
        for a, b in zip(self.points, self.points[1:]):
            self.cum.append(self.cum[-1] + haversine(a, b))
        self.length = self.cum[-1]

    def _seg(self, d):
        d = max(0.0, min(self.length, d))
        lo, hi = 0, len(self.cum) - 1
        while lo < hi - 1:
            mid = (lo + hi) // 2
            if self.cum[mid] <= d:
                lo = mid
            else:
                hi = mid
        return lo, d

    def at(self, d):
        if len(self.points) == 1:
            return self.points[0]
        i, d = self._seg(d)
        seg = self.cum[i + 1] - self.cum[i]
        t = 0.0 if seg <= 0 else (d - self.cum[i]) / seg
        a, b = self.points[i], self.points[i + 1]
        return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t

    def heading(self, d):
        if len(self.points) < 2:
            return 0.0
        i, _ = self._seg(d)
        return bearing(self.points[i], self.points[min(i + 1, len(self.points) - 1)])

    def turn_ahead(self, d, lookahead=TURN_LOOKAHEAD_M):
        """Largest change of direction (degrees) within the next lookahead meters."""
        if len(self.points) < 3:
            return 0.0
        h0 = self.heading(d)
        worst = 0.0
        step = 10.0
        x = d + step
        while x <= min(self.length, d + lookahead):
            diff = abs((self.heading(x) - h0 + 180) % 360 - 180)
            worst = max(worst, diff)
            x += step
        return worst

    def nearest(self, p):
        """Distance along the route of the vertex nearest to p."""
        best, best_d = None, 0.0
        for q, d in zip(self.points, self.cum):
            dist = haversine(p, q)
            if best is None or dist < best:
                best, best_d = dist, d
        return best_d


# ---------------------------------------------------------------------- input
_NUM = r"(-?\d{1,3}(?:\.\d+)?)"


def parse_point(text):
    """'21.0285, 105.8542', '21.0285 105.8542' or a Google Maps link -> (lat, lon) or None."""
    text = urllib.parse.unquote((text or "").strip())
    for pattern in (rf"!3d{_NUM}!4d{_NUM}", rf"@{_NUM},{_NUM}", rf"[?&](?:q|ll|query|destination|center)={_NUM},{_NUM}",
                    rf"^{_NUM}\s*[, ;]\s*{_NUM}$"):
        m = re.search(pattern, text)
        if m:
            lat, lon = float(m.group(1)), float(m.group(2))
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                return lat, lon
    return None


def load_track(path):
    """Points of a GPX (trk/rte/wpt), KML, GeoJSON or CSV (lat,lon per line) file."""
    p = Path(path)
    text = p.read_text("utf-8", "replace")
    ext = p.suffix.lower()
    if ext in (".geojson", ".json"):
        data = json.loads(text)
        coords = []

        def walk(g):
            if not g:
                return
            t = g.get("type")
            if t == "FeatureCollection":
                for f in g.get("features", []):
                    walk(f)
            elif t == "Feature":
                walk(g.get("geometry"))
            elif t == "LineString":
                coords.extend(g["coordinates"])
            elif t == "MultiLineString":
                for line in g["coordinates"]:
                    coords.extend(line)
            elif t == "Point":
                coords.append(g["coordinates"])

        walk(data)
        return [(c[1], c[0]) for c in coords]
    if ext == ".csv" or ext == ".txt":
        out = []
        for line in text.splitlines():
            pt = parse_point(line.replace("\t", ","))
            if pt:
                out.append(pt)
        return out
    root = ET.fromstring(text)
    local = lambda e: e.tag.rpartition("}")[2]  # noqa: E731
    if ext == ".kml":
        out = []
        for e in root.iter():
            if local(e) == "coordinates" and e.text:
                for tok in e.text.split():
                    parts = tok.split(",")
                    if len(parts) >= 2:
                        out.append((float(parts[1]), float(parts[0])))
        return out
    pts = {"trkpt": [], "rtept": [], "wpt": []}
    for e in root.iter():
        name = local(e)
        if name in pts:
            pts[name].append((float(e.attrib["lat"]), float(e.attrib["lon"])))
    return pts["trkpt"] or pts["rtept"] or pts["wpt"]


def osrm_route(points, timeout=20):
    """Road route through the given (lat, lon) points (OSRM public demo server)."""
    coords = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in points)
    req = urllib.request.Request(OSRM_URL.format(coords=coords), headers={"User-Agent": "iphone-mirror-gps/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    if data.get("code") != "Ok" or not data.get("routes"):
        raise ValueError(data.get("message") or data.get("code") or "no route")
    return [(c[1], c[0]) for c in data["routes"][0]["geometry"]["coordinates"]]


def save_gpx(path, points):
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<gpx version="1.1" creator="iPhone Mirror Plus" xmlns="http://www.topografix.com/GPX/1/1">',
             "<trk><trkseg>"]
    lines += [f'<trkpt lat="{lat:.7f}" lon="{lon:.7f}"/>' for lat, lon in points]
    lines += ["</trkseg></trk>", "</gpx>"]
    Path(path).write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------- drive model
class Drive(QObject):
    """Moves along a Route once per tick and reports positions."""

    moved = Signal(float, float, float, float)  # lat, lon, speed km/h, distance m
    finished = Signal()

    def __init__(self, send):
        super().__init__()
        self.send = send  # callable(lat, lon) -> None (may block: called on a worker thread)
        self.route = None
        self.dist = 0.0
        self.v = 0.0  # m/s
        self.target_kmh = 40
        self.multiplier = 1
        self.noise_m = 0.0
        self.loop = False
        self.running = False
        self._noise = (0.0, 0.0)
        self._timer = QTimer(self, interval=int(TICK_S * 1000), timeout=self._tick)
        self._q = []
        self._q_lock = threading.Lock()
        self._worker = None

    def load(self, route):
        self.stop()
        self.route = route
        self.dist = 0.0
        self.v = 0.0
        self._emit_position()

    def play(self):
        if self.route is None:
            return
        if self.dist >= self.route.length:
            self.dist = 0.0
        self.running = True
        self._timer.start()
        self._tick()

    def pause(self):
        self.running = False
        self._timer.stop()
        self.v = 0.0
        self._emit_position()  # keep sending the same point: a stopped car

    def stop(self):
        self.running = False
        self._timer.stop()
        self.v = 0.0

    def seek(self, d):
        if self.route is not None:
            self.dist = max(0.0, min(self.route.length, d))
            self._emit_position()

    def _tick(self):
        r = self.route
        if r is None:
            return
        dt = TICK_S * self.multiplier
        target = self.target_kmh / 3.6
        turn = r.turn_ahead(self.dist)
        if turn > 30:  # slow down for turns: ~20 km/h at 90 degrees, ~12 at a U-turn
            target = min(target, max(3.0, 8.0 - (turn - 30) / 30.0))
        if self.v < target:
            self.v = min(target, self.v + ACCEL * dt)
        else:
            self.v = max(target, self.v - BRAKE * dt)
        self.dist += self.v * dt
        if self.dist >= r.length:
            if self.loop and r.length > 0:
                self.dist = 0.0
            else:
                self.dist = r.length
                self.running = False
                self._timer.stop()
                self.v = 0.0
                self._emit_position()
                self.finished.emit()
                return
        self._emit_position()

    def _emit_position(self):
        if self.route is None:
            return
        lat, lon = self.route.at(self.dist)
        if self.noise_m > 0:
            # smoothed random walk, like real GPS error (it drifts, it does not jump)
            n, e = self._noise
            n = 0.7 * n + random.gauss(0, self.noise_m * 0.5)
            e = 0.7 * e + random.gauss(0, self.noise_m * 0.5)
            self._noise = (n, e)
            lat, lon = offset((lat, lon), n, e)
        self.last = (lat, lon)
        self.moved.emit(lat, lon, self.v * 3.6, self.dist)
        self._push(lat, lon)

    def resend(self):
        """After a reconnect the iPhone is back on its real GPS: send the position again."""
        last = getattr(self, "last", None)
        if last is not None and self.route is not None:
            self._push(*last)

    def _push(self, lat, lon):
        """Send on a worker thread; only the newest pending point matters."""
        with self._q_lock:
            self._q = [(lat, lon)]
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(target=self._drain, name="gps-send", daemon=True)
            self._worker.start()

    def _drain(self):
        while True:
            with self._q_lock:
                if not self._q:
                    return
                lat, lon = self._q.pop()
            try:
                self.send(lat, lon)
            except Exception as e:
                log.info("send location failed: %s", e)


# ---------------------------------------------------------------------- mini map
class RouteMap(QWidget):
    seek = Signal(float)  # distance along the route

    def __init__(self):
        super().__init__()
        self.setMinimumSize(320, 220)
        self.route = None
        self.pos = None
        self.dist = 0.0
        self.setCursor(Qt.PointingHandCursor)

    def set_route(self, route):
        self.route = route
        self.pos = route.points[0] if route and route.points else None
        self.dist = 0.0
        self.update()

    def set_position(self, lat, lon, dist):
        self.pos = (lat, lon)
        self.dist = dist
        self.update()

    def _proj(self):
        pts = self.route.points
        lats = [p[0] for p in pts]
        lons = [p[1] for p in pts]
        lat0 = (min(lats) + max(lats)) / 2
        kx = math.cos(math.radians(lat0))
        minx, maxx = min(lons) * kx, max(lons) * kx
        miny, maxy = min(lats), max(lats)
        w, h, pad = self.width(), self.height(), 18
        span = max(maxx - minx, maxy - miny, 1e-6)
        s = min((w - 2 * pad) / max(maxx - minx, span * 0.02), (h - 2 * pad) / max(maxy - miny, span * 0.02))
        cx, cy = (minx + maxx) / 2, (miny + maxy) / 2

        def f(p):
            return QPointF(w / 2 + (p[1] * kx - cx) * s, h / 2 - (p[0] - cy) * s)

        return f

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor("#15171c"))
        if not self.route or len(self.route.points) < 1:
            p.setPen(QColor(255, 255, 255, 120))
            p.drawText(self.rect(), Qt.AlignCenter, "Chưa có lộ trình")
            return
        f = self._proj()
        pts = self.route.points
        if len(pts) > 1:
            path = QPainterPath(f(pts[0]))
            for q in pts[1:]:
                path.lineTo(f(q))
            p.setPen(QPen(QColor("#4f8cff"), 4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawPath(path)
            # driven part
            done = QPainterPath(f(pts[0]))
            for q, d in zip(pts[1:], self.route.cum[1:]):
                if d > self.dist:
                    break
                done.lineTo(f(q))
            if self.pos:
                done.lineTo(f(self.route.at(self.dist)))
            p.setPen(QPen(QColor("#8a8f9c"), 4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawPath(done)
        for q, color in ((pts[0], "#30d158"), (pts[-1], "#ff453a")):
            p.setPen(QPen(QColor("white"), 2))
            p.setBrush(QColor(color))
            p.drawEllipse(f(q), 6, 6)
        if self.pos:
            p.setPen(QPen(QColor("white"), 2))
            p.setBrush(QColor("#0a84ff"))
            p.drawEllipse(f(self.pos), 8, 8)

    def mousePressEvent(self, e):
        if not self.route or len(self.route.points) < 2:
            return
        f = self._proj()
        click = e.position()
        best, best_d = None, 0.0
        for q, d in zip(self.route.points, self.route.cum):
            pt = f(q)
            dist = (pt.x() - click.x()) ** 2 + (pt.y() - click.y()) ** 2
            if best is None or dist < best:
                best, best_d = dist, d
        self.seek.emit(best_d)


# ---------------------------------------------------------------------- panel
class GpsPanel(QWidget):
    """Window with the route, the drive controls and the mini map."""

    _routed = Signal(object, str)  # points or None, error text

    def __init__(self, settings, get_server, parent=None):
        super().__init__(parent, Qt.Window)
        self.settings = settings
        self.get_server = get_server
        self.setWindowTitle("Giả lập GPS - iPhone Mirror Plus")
        self.resize(460, 620)
        self.drive = Drive(self._send)
        self.drive.moved.connect(self._on_moved)
        self.drive.finished.connect(lambda: self._status("Đã tới điểm đến."))
        self._routed.connect(self._on_routed)

        g = settings.get("gps") or {}
        self.start_in = QLineEdit(g.get("start", ""))
        self.start_in.setPlaceholderText("Điểm đi: 21.0285, 105.8542 hoặc link Google Maps")
        self.via_in = QLineEdit(g.get("via", ""))
        self.via_in.setPlaceholderText("Điểm qua (tuỳ chọn), cách nhau bằng dấu |")
        self.end_in = QLineEdit(g.get("end", ""))
        self.end_in.setPlaceholderText("Điểm đến")

        b_here = QPushButton("Đặt iPhone tại điểm đi")
        b_route = QPushButton("Tìm đường (OSRM)")
        b_file = QPushButton("Mở file GPX/KML/GeoJSON...")
        b_save = QPushButton("Lưu GPX...")
        b_real = QPushButton("Về GPS thật")
        b_here.clicked.connect(self._teleport)
        b_route.clicked.connect(self._find_route)
        b_file.clicked.connect(self._open_file)
        b_save.clicked.connect(self._save_file)
        b_real.clicked.connect(self._real_gps)

        self.speed = QSpinBox()
        self.speed.setRange(1, 200)
        self.speed.setSuffix(" km/h")
        self.speed.setValue(int(g.get("speed", 40)))
        self.speed.valueChanged.connect(self._apply_options)
        self.mult = QComboBox()
        self.mult.addItems(["x1", "x2", "x5", "x10"])
        self.mult.setCurrentText(g.get("mult", "x1"))
        self.mult.currentTextChanged.connect(self._apply_options)
        self.noise = QSpinBox()
        self.noise.setRange(0, 50)
        self.noise.setSuffix(" m nhiễu")
        self.noise.setValue(int(g.get("noise", 0)))
        self.noise.valueChanged.connect(self._apply_options)
        self.loop = QCheckBox("Lặp lại")
        self.loop.setChecked(bool(g.get("loop", False)))
        self.loop.toggled.connect(self._apply_options)

        self.b_play = QPushButton("▶ Chạy")
        self.b_pause = QPushButton("⏸ Dừng xe")
        self.b_stop = QPushButton("⏹ Về đầu")
        self.b_play.clicked.connect(self._play)
        self.b_pause.clicked.connect(self._pause)
        self.b_stop.clicked.connect(self._rewind)

        self.map = RouteMap()
        self.map.seek.connect(self.drive.seek)
        self.info = QLabel("")
        self.info.setWordWrap(True)
        self.status = QLabel("")
        self.status.setWordWrap(True)

        lay = QVBoxLayout(self)
        form = QGridLayout()
        form.addWidget(QLabel("Đi"), 0, 0)
        form.addWidget(self.start_in, 0, 1)
        form.addWidget(QLabel("Qua"), 1, 0)
        form.addWidget(self.via_in, 1, 1)
        form.addWidget(QLabel("Đến"), 2, 0)
        form.addWidget(self.end_in, 2, 1)
        lay.addLayout(form)
        row = QHBoxLayout()
        for b in (b_route, b_here):
            row.addWidget(b)
        lay.addLayout(row)
        row = QHBoxLayout()
        for b in (b_file, b_save, b_real):
            row.addWidget(b)
        lay.addLayout(row)
        row = QHBoxLayout()
        for w in (QLabel("Tốc độ"), self.speed, self.mult, self.noise, self.loop):
            row.addWidget(w)
        lay.addLayout(row)
        row = QHBoxLayout()
        for b in (self.b_play, self.b_pause, self.b_stop):
            row.addWidget(b)
        lay.addLayout(row)
        lay.addWidget(self.map, 1)
        lay.addWidget(self.info)
        lay.addWidget(self.status)
        self._apply_options()
        last = g.get("route")
        if last:
            self._load_points(last, "Lộ trình lần trước")

    # ---------------------------------------------------------------- helpers
    def _status(self, text):
        self.status.setText(text)

    def _save_settings(self):
        g = {
            "start": self.start_in.text(), "via": self.via_in.text(), "end": self.end_in.text(),
            "speed": self.speed.value(), "mult": self.mult.currentText(), "noise": self.noise.value(),
            "loop": self.loop.isChecked(),
            "route": [list(p) for p in self.drive.route.points] if self.drive.route else None,
        }
        self.settings["gps"] = g
        self.settings.save()

    def _apply_options(self):
        self.drive.target_kmh = self.speed.value()
        self.drive.multiplier = int(self.mult.currentText()[1:])
        self.drive.noise_m = float(self.noise.value())
        self.drive.loop = self.loop.isChecked()

    def _send(self, lat, lon):
        server = self.get_server()
        if server is None:
            raise ConnectionError("iPhone chưa kết nối")
        server.extra("/location", {"lat": lat, "lon": lon}, timeout=8)

    def _load_points(self, points, label):
        route = Route(points)
        if not route.points:
            self._status("Lộ trình trống.")
            return
        self.drive.load(route)
        self.map.set_route(route)
        km = route.length / 1000
        mins = route.length / max(1.0, self.speed.value() / 3.6) / 60
        self.info.setText(f"{label}: {len(route.points)} điểm, {km:.2f} km, khoảng {mins:.0f} phút ở {self.speed.value()} km/h.")
        self._save_settings()

    # ---------------------------------------------------------------- actions
    def _teleport(self):
        p = parse_point(self.start_in.text())
        if not p:
            self._status("Điểm đi chưa đúng. Nhập dạng 21.0285, 105.8542 hoặc dán link Google Maps.")
            return
        self._load_points([p], "Vị trí cố định")
        self._status("Đã đặt iPhone tại điểm đi.")

    def _find_route(self):
        pts = [parse_point(self.start_in.text())]
        pts += [parse_point(v) for v in self.via_in.text().split("|") if v.strip()]
        pts.append(parse_point(self.end_in.text()))
        if any(p is None for p in pts):
            self._status("Có điểm chưa đúng định dạng (lat, lon hoặc link Google Maps).")
            return
        self._status("Đang tìm đường...")

        def work():
            try:
                self._routed.emit(osrm_route(pts), "")
            except Exception as e:
                self._routed.emit(None, str(e))

        threading.Thread(target=work, daemon=True).start()

    def _on_routed(self, points, error):
        if points is None:
            self._status(f"Không tìm được đường: {error}")
            return
        self._load_points(points, "Tuyến đường")
        self._status("Đã có tuyến. Bấm ▶ Chạy rồi mở app dẫn đường trên iPhone.")

    def _open_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Mở lộ trình", "", "Lộ trình (*.gpx *.kml *.geojson *.json *.csv *.txt)")
        if not path:
            return
        try:
            pts = load_track(path)
        except Exception as e:
            self._status(f"Không đọc được file: {e}")
            return
        self._load_points(pts, Path(path).name)

    def _save_file(self):
        if not self.drive.route:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Lưu lộ trình", "route.gpx", "GPX (*.gpx)")
        if path:
            save_gpx(path, self.drive.route.points)
            self._status(f"Đã lưu {path}")

    def _play(self):
        if not self.drive.route:
            self._status("Chưa có lộ trình.")
            return
        self._apply_options()
        self.drive.play()
        self._status("Đang chạy.")

    def _pause(self):
        self.drive.pause()
        self._status("Xe đang dừng (vẫn giữ vị trí).")

    def _rewind(self):
        self.drive.stop()
        self.drive.seek(0.0)
        self._status("Đã về điểm đầu.")

    def _real_gps(self):
        self.drive.stop()

        def work():
            server = self.get_server()
            try:
                if server is not None:
                    server.extra("/location/clear", {}, timeout=8)
            except Exception as e:
                log.info("clear location: %s", e)

        threading.Thread(target=work, daemon=True).start()
        self._status("iPhone đã dùng lại GPS thật.")

    def _on_moved(self, lat, lon, kmh, dist):
        self.map.set_position(lat, lon, dist)
        r = self.drive.route
        left = (r.length - dist) / 1000 if r else 0
        self.setWindowTitle(f"GPS {lat:.5f}, {lon:.5f} · {kmh:.0f} km/h · còn {left:.2f} km")

    def closeEvent(self, e):
        self._save_settings()
        super().closeEvent(e)
