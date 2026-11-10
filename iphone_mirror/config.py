"""Persistent settings and identity (name, MAC, Ed25519 key) of the receiver."""
import json
import logging
import os
import secrets
import socket
from pathlib import Path

log = logging.getLogger(__name__)

APP_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "iPhoneMirror"
SETTINGS_FILE = APP_DIR / "settings.json"
LOG_FILE = APP_DIR / "iphone_mirror.log"

DEFAULTS = {
    # name shown on the iPhone in Control Center -> Screen Mirroring
    "name": "",
    # resolution requested from the iPhone; the height controls the stream size
    "width": 1920,
    "height": 1080,
    "fps": 60,
    "audio": True,
    "always_on_top": True,
    # ask in a dialog before an iPhone that is not in trusted_devices may mirror
    "require_approval": True,
    "trusted_devices": {},  # deviceID -> name, filled by "Luôn cho phép"
    # window geometry (logical pixels); 0 = automatic
    "x": None,
    "y": None,
    "phone_height": 0,
    # identity, generated on first start
    "mac": "",
    "ed25519_seed": "",
}


class Settings:
    def __init__(self, path: Path = SETTINGS_FILE, default_name: str = ""):
        self.path = path
        self.data = dict(DEFAULTS, name=default_name)
        APP_DIR.mkdir(parents=True, exist_ok=True)
        if path.exists():
            try:
                # utf-8-sig: also accept a file saved with a BOM (e.g. edited in PowerShell)
                self.data.update(json.loads(path.read_text(encoding="utf-8-sig")))
            except Exception as e:  # corrupt file: keep defaults, rewrite
                log.warning("settings.json unreadable (%s), using defaults", e)
        changed = False
        if not self.data.get("name"):
            self.data["name"] = socket.gethostname() or "Windows PC"
            changed = True
        if not self.data.get("mac"):
            mac = bytearray(secrets.token_bytes(6))
            mac[0] = (mac[0] | 0x02) & 0xFE  # locally administered, unicast
            self.data["mac"] = mac.hex()
            changed = True
        if not self.data.get("ed25519_seed"):
            self.data["ed25519_seed"] = secrets.token_hex(32)
            changed = True
        if changed:
            self.save()

    def __getitem__(self, k):
        return self.data[k]

    def __setitem__(self, k, v):
        self.data[k] = v

    def get(self, k, default=None):
        return self.data.get(k, default)

    def save(self):
        try:
            self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            log.warning("could not save settings: %s", e)

    @property
    def mac_bytes(self) -> bytes:
        return bytes.fromhex(self.data["mac"])

    @property
    def device_id(self) -> str:  # aa:bb:cc:dd:ee:ff
        return ":".join(f"{b:02x}" for b in self.mac_bytes)

    @property
    def raop_prefix(self) -> str:  # AABBCCDDEEFF
        return self.mac_bytes.hex().upper()
