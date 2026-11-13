"""BLE-Proxys anlegen: ESPHome-Dateien <raum>-ble-<nr>.yaml mit eigener Nummer und eigenen Schlüsseln.

Die Dateien landen in /config/esphome (dort liest der ESPHome Device Builder) und
binden das gemeinsame Paket common/ble-proxy.yaml ein. Pro Raum gibt es höchstens
MAX_PER_ROOM Proxys; freie Nummern (z. B. nach dem Löschen von 02) werden zuerst vergeben.
"""

from __future__ import annotations

import base64
import os
import re
import secrets

from homeassistant.core import HomeAssistant

# Raumkürzel wie bei den Raum-Panels -> Bereichsname in HA
ROOMS: dict[str, str] = {
    "fl": "Flur", "bz": "Badezimmer", "ku": "Küche", "sz": "Schlafzimmer",
    "bu": "Büro", "wz": "Wohnzimmer", "ke": "Keller", "ba": "Balkon",
}
MAX_PER_ROOM = 4
PACKAGE = "common/ble-proxy.yaml"

FILE_RE = re.compile(r"^([a-z]{2})-ble-(\d{2})\.yaml$")
_SUB_RE = {k: re.compile(rf'^\s*{k}:\s*"?([^"\n]*?)"?\s*$', re.M) for k in ("friendly_name", "api_key", "area")}

TEMPLATE = """# {friendly} – {area}. Angelegt von Home Twin (BLE anlernen → Proxys).
# Erstes Flashen per USB: ESPHome Device Builder → Install → Manual download (Factory)
# → web.esphome.io. Danach Updates per WLAN. Position im Home-Twin-Grundriss setzen.

substitutions:
  name: {name}
  friendly_name: {friendly}
  area: {area}
  api_key: "{api_key}"
  ota_password: "{ota}"
  ap_password: "{ap}"

packages:
  ble_proxy: !include {package}
"""


def _dir(hass: HomeAssistant) -> str:
    return hass.config.path("esphome")


def _read(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return {}
    out = {}
    for key, rx in _SUB_RE.items():
        m = rx.search(text)
        if m:
            out[key] = m.group(1).strip()
    return out


def list_proxies(hass: HomeAssistant) -> list[dict]:
    """Alle Proxy-Dateien nach Schema <raum>-ble-<nr>.yaml (blockierend, im Executor aufrufen)."""
    folder = _dir(hass)
    out = []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    for fn in names:
        m = FILE_RE.match(fn)
        if not m:
            continue
        room, nr = m.group(1), int(m.group(2))
        info = _read(os.path.join(folder, fn))
        out.append({
            "file": fn, "name": fn[:-5], "room": room, "room_name": ROOMS.get(room, room.upper()), "nr": nr,
            "friendly": info.get("friendly_name") or f"{room.upper()}-{nr:02d} BLE-Proxy",
            "area": info.get("area"), "api_key": info.get("api_key"),
        })
    return out


def create_proxy(hass: HomeAssistant, room: str) -> dict:
    """Nächste freie Nummer im Raum vergeben und Datei schreiben (blockierend)."""
    room = room.lower()
    if room not in ROOMS:
        raise ValueError(f"Unbekannter Raum: {room}")
    used = {p["nr"] for p in list_proxies(hass) if p["room"] == room}
    free = [n for n in range(1, MAX_PER_ROOM + 1) if n not in used]
    if not free:
        raise ValueError(f"{ROOMS[room]} hat schon {MAX_PER_ROOM} Proxys")
    nr = free[0]
    name = f"{room}-ble-{nr:02d}"
    friendly = f"{room.upper()}-{nr:02d} BLE-Proxy"
    area = ROOMS[room]
    api_key = base64.b64encode(secrets.token_bytes(32)).decode()
    folder = _dir(hass)
    if not os.path.isfile(os.path.join(folder, PACKAGE)):
        raise ValueError(f"Gemeinsames Paket fehlt: /config/esphome/{PACKAGE}")
    path = os.path.join(folder, f"{name}.yaml")
    # "x" = nie eine vorhandene Datei überschreiben
    with open(path, "x", encoding="utf-8") as fh:
        fh.write(TEMPLATE.format(name=name, friendly=friendly, area=area, api_key=api_key,
                                 ota=secrets.token_hex(16), ap=secrets.token_hex(8), package=PACKAGE))
    return {"file": f"{name}.yaml", "name": name, "room": room, "room_name": area, "nr": nr,
            "friendly": friendly, "area": area, "api_key": api_key}
