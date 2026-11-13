"""Decoder für BLE-Advertisements (reines Python, ohne HA-Abhängigkeiten).

Erkennt iBeacon, AltBeacon, Eddystone (UID/URL/TLM/EID) und die Frames von
KKM-Beacons (BlueCharm, z. B. BC021): KSensor v1/v2, System, Zusatzdaten
(Service 0x2080) und verschlüsselte Varianten.
Die KKM-Formate sind aus dem offiziellen SDK übernommen
(github.com/kkmhogen/android_kbeaconlib2, MIT-Lizenz, KBAdvPacket*.java).

decode() liefert {frame_key: {"l": Anzeige-Name, "v": [{"k","l","v","u"?}, ...]}}.
Die Werte-Listen sind selbstbeschreibend, damit die Karte generisch bleibt.
"""

from __future__ import annotations

APPLE = 0x004C
MICROSOFT = 0x0006
KKM = 0x0A53

UUID_EDDYSTONE = "0000feaa-0000-1000-8000-00805f9b34fb"
UUID_KKM_EXT = "00002080-0000-1000-8000-00805f9b34fb"

# Bekannte Firmen-IDs (Bluetooth SIG) – nur zur Anzeige
COMPANIES = {
    0x0006: "Microsoft", 0x004C: "Apple", 0x0075: "Samsung", 0x00E0: "Google",
    0x0087: "Garmin", 0x0157: "Huami (Amazfit)", 0x038F: "Xiaomi", 0x0499: "Ruuvi",
    0x0A53: "KKM (BlueCharm)", 0x0059: "Nordic", 0x000D: "Texas Instruments",
    0x0131: "Cypress", 0x02E5: "Espressif", 0x0171: "Amazon", 0x027D: "Huawei",
    0x0002: "Intel", 0x0001: "Nokia", 0x0118: "Radius Networks", 0x0969: "Woan (SwitchBot)",
}

# Bekannte 16-Bit-Service-UUIDs – nur zur Anzeige
SERVICES = {
    "feaa": "Eddystone", "2080": "KKM-Zusatzdaten", "fcd2": "BTHome", "fe95": "Xiaomi MiBeacon",
    "fe2c": "Google Fast Pair", "fd6f": "Corona-Warn/Exposure", "feed": "Tile", "fe9f": "Google",
    "fd5a": "Samsung SmartTag", "fe07": "Sonos", "fe03": "Amazon", "180f": "Batterie",
    "181a": "Umweltsensor", "180d": "Herzfrequenz", "1812": "HID (Tastatur/Maus)",
    "fef3": "Google", "fe78": "HP", "fdab": "Xiaomi", "fd3d": "Woan (SwitchBot)",
    "181b": "Körperzusammensetzung", "181d": "Waage", "fe0f": "Philips Hue", "fe59": "Nordic DFU",
}

APPLE_TYPES = {
    0x01: "Apple-Overflow", 0x02: "iBeacon", 0x03: "AirPrint", 0x05: "AirDrop", 0x06: "HomeKit",
    0x07: "AirPods (Proximity Pairing)", 0x08: "Hey Siri", 0x09: "AirPlay-Ziel", 0x0A: "AirPlay-Quelle",
    0x0B: "Apple Watch (Magic Switch)", 0x0C: "Handoff", 0x0D: "WLAN-Einstellungen", 0x0E: "Hotspot",
    0x0F: "Nearby Action", 0x10: "Nearby Info", 0x12: "Find My (Wo ist?)", 0x16: "Nearby Action",
}

EDDY_URL_PREFIX = ["http://www.", "https://www.", "http://", "https://"]
EDDY_URL_CODES = [".com/", ".org/", ".edu/", ".net/", ".info/", ".biz/", ".gov/",
                  ".com", ".org", ".edu", ".net", ".info", ".biz", ".gov"]

KSENSOR_MASK = (  # (Bit, Schlüssel, Länge in Bytes) in Sende-Reihenfolge
    (0x001, "voltage", 2), (0x002, "temp", 2), (0x004, "hum", 2), (0x008, "acc", 6),
    (0x010, "alarm", 1), (0x020, "pir", 1), (0x040, "lux", 2), (0x080, "voc", 5),
    (0x200, "co2", 3), (0x400, "record", 3),
)


def _v(key: str, label: str, value, unit: str | None = None) -> dict:
    d = {"k": key, "l": label, "v": value}
    if unit:
        d["u"] = unit
    return d


def _u16(b: bytes, i: int) -> int:
    return (b[i] << 8) | b[i + 1]


def _s16(b: bytes, i: int) -> int:
    v = _u16(b, i)
    return v - 0x10000 if v >= 0x8000 else v


def _s8(x: int) -> int:
    return x - 0x100 if x >= 0x80 else x


def _fix88(b: bytes, i: int) -> float:
    """Vorzeichenbehaftete 8.8-Festkommazahl (Temperatur in Eddystone-TLM/KSensor v1)."""
    return round(_s16(b, i) / 256.0, 2)


def _uuid(b: bytes) -> str:
    h = b.hex()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}".upper()


def short_uuid(uuid: str) -> str:
    """0000feaa-0000-1000-8000-00805f9b34fb -> feaa (sonst unverändert)."""
    u = uuid.lower()
    if len(u) == 36 and u.startswith("0000") and u.endswith("-0000-1000-8000-00805f9b34fb"):
        return u[4:8]
    return u


def address_kind(address: str) -> str:
    """Art der Adresse aus den obersten zwei Bits (nur gültig bei Random-Adressen).

    rpa = wechselnd (Handys, Uhren), static = fest zufällig, public = Herstelleradresse/unbekannt.
    """
    try:
        top = int(address[:2], 16) >> 6
    except ValueError:
        return "public"
    return {1: "rpa", 3: "static"}.get(top, "public")


# ---------------------------------------------------------------- Einzel-Decoder

def _ibeacon(d: bytes) -> dict | None:
    if len(d) < 23 or d[0] != 0x02 or d[1] != 0x15:
        return None
    return {"l": "iBeacon", "v": [
        _v("uuid", "UUID", _uuid(d[2:18])),
        _v("major", "Major", _u16(d, 18)),
        _v("minor", "Minor", _u16(d, 20)),
        _v("tx1m", "Sendeleistung @1 m", _s8(d[22]), "dBm"),
    ]}


def _apple(d: bytes) -> dict | None:
    types, i = [], 0
    while i + 1 < len(d):
        t, ln = d[i], d[i + 1]
        if t == 0x02:  # iBeacon wird separat dekodiert
            return None
        types.append(APPLE_TYPES.get(t, f"Typ 0x{t:02X}"))
        i += 2 + ln
    if not types:
        return None
    return {"l": "Apple Continuity", "v": [_v("apple_types", "Meldungen", ", ".join(dict.fromkeys(types)))]}


def _altbeacon(cid: int, d: bytes) -> dict | None:
    if len(d) < 24 or d[0] != 0xBE or d[1] != 0xAC:
        return None
    return {"l": "AltBeacon", "v": [
        _v("id1", "ID 1", _uuid(d[2:18])),
        _v("id2", "ID 2", _u16(d, 18)),
        _v("id3", "ID 3", _u16(d, 20)),
        _v("tx1m", "Referenz-RSSI @1 m", _s8(d[22]), "dBm"),
        _v("company", "Hersteller", COMPANIES.get(cid, f"0x{cid:04X}")),
    ]}


def _eddy_uid(d: bytes) -> dict | None:
    if len(d) < 18:
        return None
    return {"l": "Eddystone-UID", "v": [
        _v("namespace", "Namespace", d[2:12].hex().upper()),
        _v("instance", "Instanz", d[12:18].hex().upper()),
        _v("tx0m", "Sendeleistung @0 m", _s8(d[1]), "dBm"),
    ]}


def _eddy_url(d: bytes) -> dict | None:
    if len(d) < 3:
        return None
    url = EDDY_URL_PREFIX[d[2]] if d[2] < len(EDDY_URL_PREFIX) else ""
    for c in d[3:]:
        url += EDDY_URL_CODES[c] if c < len(EDDY_URL_CODES) else chr(c)
    return {"l": "Eddystone-URL", "v": [_v("url", "URL", url), _v("tx0m", "Sendeleistung @0 m", _s8(d[1]), "dBm")]}


def _eddy_tlm(d: bytes) -> dict | None:
    if len(d) < 14:
        return None
    if d[1] != 0x00:
        return {"l": "Eddystone-TLM", "v": [_v("tlm_enc", "Inhalt", "verschlüsselt (eTLM)")]}
    vals = []
    batt = _u16(d, 2)
    if batt:
        vals.append(_v("battery_mv", "Batteriespannung", batt, "mV"))
    if d[4] != 0x80:  # 0x8000 = keine Temperatur
        vals.append(_v("temperature", "Temperatur", _fix88(d, 4), "°C"))
    adv = int.from_bytes(d[6:10], "big")
    sec = int.from_bytes(d[10:14], "big") // 10
    vals += [_v("adv_count", "Gesendete Pakete", adv), _v("uptime_s", "Laufzeit", sec, "s")]
    return {"l": "Eddystone-TLM", "v": vals}


def _kkm_system(d: bytes) -> dict | None:
    if len(d) < 11:
        return None
    mac = ":".join(f"{x:02X}" for x in d[3:9])
    return {"l": "KKM System", "v": [
        _v("model", "Modell-Nr.", d[1]),
        _v("battery_pct", "Batterie", min(d[2], 100), "%"),
        _v("mac", "MAC (laut Beacon)", mac),
        _v("firmware", "Firmware", f"{d[9]}.{d[10]}"),
    ]}


def _acc(vals: list, b: bytes, i: int) -> None:
    vals += [_v("acc_x", "Beschleunigung X", _s16(b, i), "mg"),
             _v("acc_y", "Beschleunigung Y", _s16(b, i + 2), "mg"),
             _v("acc_z", "Beschleunigung Z", _s16(b, i + 4), "mg")]


def _ksensor_v1(d: bytes) -> dict | None:
    if len(d) < 3:
        return None
    mask = _u16(d, 1)
    b, i, vals = d[3:], 0, []
    for bit, key, ln in KSENSOR_MASK:
        if not mask & bit:
            continue
        if i + ln > len(b):
            break
        if key == "voltage":
            vals.append(_v("battery_mv", "Batteriespannung", _u16(b, i), "mV"))
        elif key == "temp":
            vals.append(_v("temperature", "Temperatur", _fix88(b, i), "°C"))
        elif key == "hum":
            if b[i] < 0x80:
                vals.append(_v("humidity", "Luftfeuchte", _fix88(b, i), "%"))
            else:
                vals.append(_v("cpu_temp", "Chip-Temperatur", _s8(b[i + 1]), "°C"))
        elif key == "acc":
            _acc(vals, b, i)
        elif key == "alarm":
            vals.append(_v("alarm", "Alarm/Taste", b[i]))
        elif key == "pir":
            vals.append(_v("pir", "Bewegung (PIR)", b[i]))
        elif key == "lux":
            vals.append(_v("lux", "Helligkeit", _u16(b, i), "lx"))
        elif key == "voc":
            vals += [_v("voc", "VOC-Index", _u16(b, i + 1)), _v("nox", "NOx-Index", _u16(b, i + 3))]
        elif key == "co2":
            vals.append(_v("co2", "CO₂", _u16(b, i + 1), "ppm"))
        elif key == "record":
            vals.append(_v("records", "Neue Aufzeichnungen", _u16(b, i + 1)))
        i += ln
    return {"l": "KSensor", "v": vals}


def _ksensor_v2(d: bytes) -> dict | None:
    if len(d) < 5:
        return None
    vals = [_v("model", "Modell-Nr.", d[1])]
    b = d[2:]
    vals.append(_v("battery_mv", "Batteriespannung", _u16(b, 0), "mV"))
    vals.append(_v("cpu_temp", "Chip-Temperatur", _s8(b[2]), "°C"))
    i = 3
    while i + 3 <= len(b):
        ln, typ = b[i] - 1, b[i + 1]
        i += 2
        if ln < 0 or i + ln > len(b):
            break
        if typ == 0x2 and ln >= 2:
            vals.append(_v("temperature", "Temperatur", round(_s16(b, i) / 10, 1), "°C"))
        elif typ == 0x3 and ln >= 3:
            vals += [_v("temperature", "Temperatur", round(_s16(b, i) / 10, 1), "°C"),
                     _v("humidity", "Luftfeuchte", b[i + 2], "%")]
        elif typ == 0x4 and ln >= 6:
            _acc(vals, b, i)
        elif typ == 0x5 and ln >= 1:
            vals.append(_v("alarm", "Alarm/Taste", b[i]))
        elif typ == 0x6 and ln >= 1:
            vals.append(_v("pir", "Bewegung (PIR)", b[i]))
        elif typ == 0x7 and ln >= 2:
            vals.append(_v("lux", "Helligkeit", _u16(b, i), "lx"))
        elif typ == 0x8 and ln >= 5:
            vals += [_v("voc", "VOC-Index", _u16(b, i + 1)), _v("nox", "NOx-Index", _u16(b, i + 3))]
        elif typ == 0x9 and ln >= 3:
            vals.append(_v("co2", "CO₂", _u16(b, i + 1), "ppm"))
        elif typ == 0xA and ln >= 3:
            vals.append(_v("records", "Neue Aufzeichnungen", _u16(b, i + 1)))
        else:
            vals.append(_v(f"sensor_0x{typ:02x}", f"Sensor-Typ 0x{typ:02X}", b[i:i + ln].hex().upper()))
        i += ln
    return {"l": "KSensor", "v": vals}


def _kkm_encrypted(label: str) -> dict:
    return {"l": label, "v": [_v("encrypted", "Inhalt", "verschlüsselt (Passwort nötig)")]}


# ---------------------------------------------------------------- Gesamt

def decode(mfg: dict[int, bytes], svc: dict[str, bytes]) -> dict[str, dict]:
    """Alle erkennbaren Frames eines Advertisements."""
    out: dict[str, dict] = {}
    for cid, d in mfg.items():
        d = bytes(d)
        if not d:
            continue
        if cid == APPLE:
            f = _ibeacon(d)
            if f:
                out["ibeacon"] = f
            else:
                f = _apple(d)
                if f:
                    out["apple"] = f
        elif cid == KKM:
            if d[0] == 0x24:
                f = _ksensor_v2(d)
                if f:
                    out["ksensor"] = f
            elif d[0] in (0x06, 0x07):
                out["ksensor"] = _kkm_encrypted("KSensor")
            elif d[0] == 0x03:
                out["ebeacon"] = _kkm_encrypted("KKM EBeacon")
        elif cid == MICROSOFT and d[0] == 0x03:
            out["swiftpair"] = {"l": "Microsoft Swift Pair", "v": [_v("ms", "Meldung", "Kopplungsbereit")]}
        f = _altbeacon(cid, d)
        if f:
            out["altbeacon"] = f

    for uuid, d in svc.items():
        d = bytes(d)
        su = short_uuid(uuid)
        if su == "feaa" and d:
            t = d[0]
            f = None
            key = None
            if t == 0x00:
                f, key = _eddy_uid(d), "eddy_uid"
            elif t == 0x10:
                f, key = _eddy_url(d), "eddy_url"
            elif t == 0x20:
                f, key = _eddy_tlm(d), "eddy_tlm"
            elif t == 0x21:
                f, key = _ksensor_v1(d), "ksensor"
            elif t == 0x22:
                f, key = _kkm_system(d), "kkm_system"
            elif t == 0x30:
                f, key = {"l": "Eddystone-EID", "v": [_v("eid", "EID", d[2:10].hex().upper())]}, "eddy_eid"
            if f and key:
                out[key] = f
        elif su == "2080" and len(d) > 2:
            out["kkm_ext"] = {"l": "KKM Zusatzdaten", "v": [_v("battery_pct", "Batterie", min(d[0], 100), "%")]}
    return out


def classify(frames: dict, mfg_ids, svc_short, name: str | None) -> tuple[str, str]:
    """(Kategorie, Anzeige-Art). Kategorie: beacon | apple | device."""
    keys = set(frames)
    if keys & {"ksensor", "kkm_system", "kkm_ext", "ebeacon"} or KKM in mfg_ids:
        return "beacon", "BlueCharm/KKM-Beacon"
    if "ibeacon" in keys:
        return "beacon", "iBeacon"
    if keys & {"eddy_uid", "eddy_url", "eddy_tlm", "eddy_eid"}:
        return "beacon", "Eddystone-Beacon"
    if "altbeacon" in keys:
        return "beacon", "AltBeacon"
    if APPLE in mfg_ids or "apple" in keys:
        return "apple", "Apple-Gerät"
    for s in svc_short:
        if s in SERVICES and s not in ("180f",):
            return "device", SERVICES[s]
    for cid in mfg_ids:
        if cid in COMPANIES:
            return "device", f"{COMPANIES[cid]}-Gerät"
    return "device", "BLE-Gerät"
