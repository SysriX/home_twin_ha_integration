"""BLE-Erfassung für Home Twin.

Sammelt alle Bluetooth-Advertisements, die HA von seinen Scannern bekommt
(ESPHome-Bluetooth-Proxys, lokale Adapter), dekodiert die Frames (ble_decode)
und merkt sich pro Gerät die Signalstärke je Scanner – Grundlage für die
spätere Ortung im Grundriss.

Unbekannte Geräte leben nur hier im Speicher und im Live-Strom an die Karte
(home_twin/ble/subscribe) – in HA entstehen dafür KEINE Entitäten.

Zwei Quellen:
  - async_register_callback (alle Adressen): Frames/Name, sobald sich Daten ändern
  - Abfrage aller Scanner jede Sekunde: RSSI je Scanner (auch von Scannern,
    deren Empfang HA für die "beste Quelle" verworfen hat)
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from homeassistant.const import EVENT_COMPONENT_LOADED, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

from . import ble_decode as dec
from .ble_position import PlanIndex, locate, norm_name
from .ble_tags import KINDS, SIGNAL_UPD

_LOGGER = logging.getLogger(__name__)

TICK = timedelta(seconds=1)
EXPIRE_S = 180          # Gerät verschwindet aus der Liste, wenn so lange nicht gesehen
SCANNER_STALE_S = 30    # Empfang eines Scanners gilt danach als weg
RSSI_ALPHA = 0.3        # Glättung (exponentieller Mittelwert)
MAX_DEVICES = 3000      # Notbremse bei sehr vielen Geräten


class BleTracker:
    """Live-Bild aller BLE-Geräte und Scanner."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._devs: dict[str, dict[str, Any]] = {}
        self._dirty: set[str] = set()
        self._removed: set[str] = set()
        self._subs: set[Callable[[dict], None]] = set()
        self._unsubs: list[Callable[[], None]] = []
        self._scanners: dict[str, dict[str, Any]] = {}
        self._scanner_sig = ""
        self.running = False
        self.error: str | None = None
        self.tagstore = None       # TagStore, von __init__ gesetzt
        self._ticks = 0
        self.mgr = None            # TwinManager (Plan für die Ortung), von __init__ gesetzt
        self._idx = PlanIndex(None)
        self._idx_rev: Any = None
        self._ciphers: dict[str, Any] = {}      # Tag-Schlüssel "IRK-…" -> AES-Cipher
        self._alias: dict[str, str | None] = {}  # echte Adresse -> Tag-Schlüssel (None = geprüft, kein Treffer)

    # ---------------------------------------------------------------- Start/Stopp
    @callback
    def async_start(self) -> None:
        if self.running:
            return
        if "bluetooth" not in self.hass.config.components:
            # Bluetooth-Integration kommt evtl. später (erster ESPHome-Proxy)
            @callback
            def _loaded(event: Event) -> None:
                if event.data.get("component") == "bluetooth":
                    unsub()
                    self.async_start()

            unsub = self.hass.bus.async_listen(EVENT_COMPONENT_LOADED, _loaded)
            self._unsubs.append(unsub)
            self.error = "Bluetooth-Integration nicht geladen (noch kein Proxy/Adapter)"
            _LOGGER.info("Home Twin BLE: warte auf die Bluetooth-Integration")
            return
        from homeassistant.components import bluetooth

        self._unsubs.append(bluetooth.async_register_callback(
            self.hass, self._on_adv, None, bluetooth.BluetoothScanningMode.PASSIVE
        ))
        self._unsubs.append(async_track_time_interval(self.hass, self._tick, TICK))
        # listen_once meldet sich nach dem Auslösen selbst ab – nicht in _unsubs aufnehmen
        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._stop)
        self.running = True
        self.error = None
        _LOGGER.info("Home Twin BLE: Erfassung gestartet")

    @callback
    def _stop(self, _event: Event | None = None) -> None:
        for u in self._unsubs:
            try:
                u()
            except Exception:  # noqa: BLE001 – beim Herunterfahren egal
                pass
        self._unsubs.clear()
        self.running = False

    # ---------------------------------------------------------------- Empfang
    # ---------------------------------------------------------------- IRK (iPhones & Co.)
    @callback
    def reload_irks(self) -> None:
        """IRKs der hinzugefügten Geräte neu laden (nach Hinzufügen/Entfernen)."""
        ciphers: dict[str, Any] = {}
        if self.tagstore is not None:
            try:
                from bluetooth_data_tools import get_cipher_for_irk
            except ImportError:  # pragma: no cover – in HA immer vorhanden
                get_cipher_for_irk = None
            for key, tag in self.tagstore.tags.items():
                irk = tag.get("irk")
                if irk and get_cipher_for_irk:
                    try:
                        ciphers[key] = get_cipher_for_irk(bytes.fromhex(irk))
                    except ValueError:
                        _LOGGER.warning("BLE: ungültiger IRK bei %s", key)
        self._ciphers = ciphers
        self._alias.clear()

    def _key(self, address: str) -> str:
        """Wechselnde Adresse einem per IRK angelernten Gerät zuordnen (Ergebnis wird gemerkt)."""
        if not self._ciphers:
            return address
        hit = self._alias.get(address, False)
        if hit is False:
            hit = None
            if dec.address_kind(address) == "rpa":
                from bluetooth_data_tools import resolve_private_address

                for key, cipher in self._ciphers.items():
                    if resolve_private_address(cipher, address):
                        hit = key
                        break
            if len(self._alias) > 20000:
                self._alias.clear()
            self._alias[address] = hit
        return hit or address

    @callback
    def _on_adv(self, info: Any, _change: Any) -> None:
        now = time.monotonic()
        key = self._key(info.address)
        rec = self._ingest(key, info.name, info.manufacturer_data, info.service_data,
                           info.service_uuids, info.tx_power, getattr(info, "connectable", None), now,
                           real=info.address)
        if rec is not None:
            self._rssi(rec, info.source, info.rssi, getattr(info, "time", None) or now)

    def _ingest(self, address: str, name: str | None, mfg: dict, svc: dict, uuids: list,
                tx_power: int | None, connectable: bool | None, now: float, real: str | None = None) -> dict | None:
        rec = self._devs.get(address)
        if rec is None:
            if len(self._devs) >= MAX_DEVICES:
                return None
            rec = self._devs[address] = {
                "a": address, "n": None, "ra": real or address,
                "ak": "irk" if real and real != address else dec.address_kind(address),
                "fs": time.time(), "ls_m": now, "f": {}, "mfg": {}, "svc": {}, "su": [],
                "tx": None, "c": None, "s": {}, "_raw": None, "k": "device", "t": "BLE-Gerät",
            }
            self._removed.discard(address)
        rec["ls_m"] = max(rec["ls_m"], now)
        if real and real != rec.get("ra"):
            rec["ra"] = real          # aktuelle (wechselnde) Adresse des per IRK erkannten Geräts
            self._dirty.add(address)
        if name and name != (real or address).replace(":", "-") and name != rec["n"]:
            rec["n"] = name
            self._dirty.add(address)
        if tx_power is not None and tx_power != rec["tx"]:
            rec["tx"] = tx_power
        if connectable:
            rec["c"] = True

        raw = (tuple(sorted((k, bytes(v)) for k, v in mfg.items())),
               tuple(sorted((k, bytes(v)) for k, v in svc.items())), tuple(uuids or ()))
        if raw != rec["_raw"]:
            rec["_raw"] = raw
            for cid, d in mfg.items():
                rec["mfg"][f"{cid:04X}"] = bytes(d).hex().upper()
            for u, d in svc.items():
                rec["svc"][dec.short_uuid(u)] = bytes(d).hex().upper()
            su = [dec.short_uuid(u) for u in (uuids or ())]
            rec["su"] = sorted(set(rec["su"]) | set(su))
            try:
                frames = dec.decode(mfg, svc)
            except Exception as err:  # noqa: BLE001 – kaputte Pakete dürfen nichts stoppen
                _LOGGER.debug("BLE-Decoder %s: %s", address, err)
                frames = {}
            wall = time.time()
            for key, fr in frames.items():
                rec["f"][key] = {**fr, "ts": wall}
            rec["k"], rec["t"] = dec.classify(rec["f"], {int(c, 16) for c in rec["mfg"]}, rec["su"], rec["n"])
            self._dirty.add(address)
        return rec

    def _rssi(self, rec: dict, source: str, rssi: int | None, ts: float) -> None:
        if rssi is None or source is None:
            return
        s = rec["s"].get(source)
        if s is None:
            rec["s"][source] = {"r": rssi, "avg": float(rssi), "ts": ts}
        elif ts > s["ts"]:
            s["r"] = rssi
            s["avg"] = s["avg"] + RSSI_ALPHA * (rssi - s["avg"])
            s["ts"] = ts
        else:
            return
        rec["ls_m"] = max(rec["ls_m"], ts)
        self._dirty.add(rec["a"])

    # ---------------------------------------------------------------- Takt
    @callback
    def _tick(self, _now: Any = None) -> None:
        from homeassistant.components import bluetooth

        now = time.monotonic()
        scanners: dict[str, dict] = {}
        try:
            current = list(bluetooth.async_current_scanners(self.hass))
        except Exception as err:  # noqa: BLE001
            self.error = f"Scanner-Abfrage: {err}"
            current = []
        for sc in current:
            src = getattr(sc, "source", None)
            if not src:
                continue
            try:
                last = sc.time_since_last_detection()
            except Exception:  # noqa: BLE001
                last = None
            scanners[src] = {"src": src, "n": getattr(sc, "name", src), "c": bool(getattr(sc, "connectable", False)),
                             "last": round(last, 1) if last is not None and last < 1e6 else None}
            try:
                stamps = sc.discovered_device_timestamps
                seen = sc.discovered_devices_and_advertisement_data
            except Exception:  # noqa: BLE001 – ältere/abweichende Scanner
                continue
            for real, (_dev, adv) in seen.items():
                ts = stamps.get(real)
                if ts is None or now - ts > SCANNER_STALE_S:
                    continue
                addr = self._key(real)
                rec = self._devs.get(addr)
                if rec is None:
                    rec = self._ingest(addr, adv.local_name, adv.manufacturer_data, adv.service_data,
                                       adv.service_uuids, adv.tx_power, None, ts, real=real)
                    if rec is None:
                        continue
                self._rssi(rec, src, adv.rssi, ts)
        sig = repr(sorted((k, v["n"], v["c"], v["last"] is not None and v["last"] < SCANNER_STALE_S)
                          for k, v in scanners.items()))
        scanners_changed = sig != self._scanner_sig
        self._scanner_sig = sig
        self._scanners = scanners

        # Abgelaufenes entfernen
        for addr, rec in list(self._devs.items()):
            for src in [s for s, v in rec["s"].items() if now - v["ts"] > SCANNER_STALE_S]:
                del rec["s"][src]
                self._dirty.add(addr)
            if now - rec["ls_m"] > EXPIRE_S and not self._adopted(addr):
                del self._devs[addr]
                self._dirty.discard(addr)
                self._removed.add(addr)

        # Ortung: Proxys aus dem Plan, Position für alle Geräte mit neuen Daten
        self._ticks += 1
        self._plan_index()
        if self._idx.proxies:
            for addr in list(self._dirty):
                rec = self._devs.get(addr)
                if rec is not None:
                    self._position(rec, now)

        # Hinzugefügte Tags: Entitäten bei neuen Daten, sonst alle 15 s (Verfügbarkeit/Alter)
        if self.tagstore is not None and self.tagstore.tags:
            every = self._ticks % 15 == 0
            for addr in self.tagstore.tags:
                if every or addr in self._dirty:
                    async_dispatcher_send(self.hass, SIGNAL_UPD.format(addr))

        if not self._subs:
            self._dirty.clear()
            self._removed.clear()
            return
        if self._dirty or self._removed or scanners_changed:
            msg: dict[str, Any] = {
                "upd": [self._payload(self._devs[a], now) for a in self._dirty if a in self._devs],
                "del": sorted(self._removed),
            }
            if scanners_changed:
                msg["scanners"] = list(scanners.values())
            self._dirty.clear()
            self._removed.clear()
            for cb in list(self._subs):
                cb(msg)

    # ---------------------------------------------------------------- Ausgabe
    @staticmethod
    def _wall(ts_mono: float, now_mono: float) -> float:
        return round(time.time() - (now_mono - ts_mono), 1)

    # ---------------------------------------------------------------- Ortung
    def _plan_index(self) -> None:
        """Proxy-Positionen neu einlesen, wenn sich der Plan geändert hat."""
        if self.mgr is None:
            return
        row = (getattr(self.mgr, "plans", None) or {}).get("main") or {}
        # Proxy-Namen (Scanner-Name und HA-Gerätename) als Rückfall, wenn im Plan kein Scanner gewählt ist
        names: dict[str, str] = {}
        for src, sc in self._scanners.items():
            names[norm_name(sc.get("n"))] = src
            try:
                from .ble_entity import proxy_name
                names.setdefault(norm_name(proxy_name(self.hass, self.mgr, src)), src)
            except Exception:  # noqa: BLE001
                pass
        rev = (row.get("revision"), id(row.get("plan")), tuple(sorted(names.items())))
        if rev != self._idx_rev:
            self._idx_rev = rev
            self._idx = PlanIndex(row.get("plan"), names)
            for rec in self._devs.values():
                rec.pop("pos", None)
            self._dirty.update(self._devs)

    def _position(self, rec: dict, now: float) -> None:
        obs = {src: v["avg"] for src, v in rec["s"].items() if now - v["ts"] < 15}
        ref = None
        ib = rec["f"].get("ibeacon")
        if ib:
            ref = next((v["v"] for v in ib.get("v") or [] if v.get("k") == "tx1m"), None)
        pos = locate(self._idx, obs, ref) if obs else None
        old = rec.get("pos")
        if pos and old and old.get("f") == pos["f"]:
            # glätten, damit der Punkt nicht springt
            for k in ("x", "y", "acc"):
                pos[k] = round(old[k] + 0.4 * (pos[k] - old[k]), 2)
            pos["room"], pos["rn"] = self._idx.room_at(pos["f"], pos["x"], pos["y"])
        rec["pos"] = pos

    def tag_info(self, key: str, now: float | None = None) -> dict:
        """Live-Stand eines hinzugefügten Geräts: Raum, Etage, Position, Proxy, Signal, Alter."""
        from .ble_entity import proxy_area, proxy_name

        now = time.monotonic() if now is None else now
        tag = (self.tagstore.tags.get(key) if self.tagstore is not None else None) or {}
        rec = self._devs.get(key)
        best = self.best(key)
        pos = (rec or {}).get("pos")
        room = pos.get("rn") if pos and pos.get("rn") else (proxy_area(self.hass, self.mgr, best["src"]) if best else None)
        return {
            "key": key, "name": tag.get("name"), "kind": tag.get("kind"), "person": tag.get("person"),
            "room": room, "proxy": proxy_name(self.hass, self.mgr, best["src"]) if best else None,
            "rssi": round(best["avg"]) if best else None,
            "age": round(now - rec["ls_m"], 1) if rec else None,
            "floor": pos.get("f") if pos else None,
            "x": pos.get("x") if pos else None, "y": pos.get("y") if pos else None,
        }

    def tag_status(self) -> list[dict]:
        """Hinzugefügte Geräte mit aktuellem Raum, Signal und Alter – für home_twin.ble_status."""
        now = time.monotonic()
        return [self.tag_info(key, now) for key in (self.tagstore.tags if self.tagstore is not None else {})]

    @property
    def proxies(self) -> dict[str, dict]:
        return self._idx.proxies

    def _adopted(self, addr: str) -> bool:
        return self.tagstore is not None and addr in self.tagstore.tags

    @callback
    def mark_dirty(self, addr: str) -> None:
        """Tag-Name o. ä. geändert -> Gerät beim nächsten Takt neu an die Karten senden."""
        if addr in self._devs:
            self._dirty.add(addr)

    # ---------------------------------------------------------------- Werte für Entitäten
    def rec(self, addr: str) -> dict | None:
        return self._devs.get(addr)

    def last_seen(self, addr: str) -> float | None:
        """Zuletzt gesehen als Unix-Zeit (Sekunden) oder None."""
        rec = self._devs.get(addr)
        return self._wall(rec["ls_m"], time.monotonic()) if rec else None

    def best(self, addr: str) -> dict | None:
        """Stärkster Empfang: {src, avg, r, age}."""
        rec = self._devs.get(addr)
        if not rec or not rec["s"]:
            return None
        now = time.monotonic()
        src, v = max(rec["s"].items(), key=lambda kv: kv[1]["avg"])
        return {"src": src, "avg": v["avg"], "r": v["r"], "age": now - v["ts"]}

    def values(self, addr: str) -> dict[str, dict]:
        """Alle dekodierten Werte, je Schlüssel der neueste Frame gewinnt: {k: {v, l, u, ts, frame}}."""
        rec = self._devs.get(addr)
        out: dict[str, dict] = {}
        if not rec:
            return out
        for fkey, fr in rec["f"].items():
            for v in fr.get("v") or []:
                k = v.get("k")
                if k and (k not in out or fr["ts"] >= out[k]["ts"]):
                    out[k] = {"v": v.get("v"), "l": v.get("l"), "u": v.get("u"), "ts": fr["ts"], "frame": fkey}
        return out

    def scanner_name(self, src: str) -> str:
        sc = self._scanners.get(src)
        return sc["n"] if sc else src

    def _payload(self, rec: dict, now: float) -> dict:
        tag = self.tagstore.tags.get(rec["a"]) if self.tagstore is not None else None
        return {
            "tag": {"name": tag.get("name"), "kind": tag.get("kind"), "icon": self.tagstore.icon(rec["a"]),
                    "image": tag.get("image"), "person": tag.get("person")} if tag else None,
            "a": rec["a"], "ra": rec.get("ra"), "n": rec["n"], "ak": rec["ak"], "k": rec["k"], "t": rec["t"],
            "fs": round(rec["fs"], 1), "ls": self._wall(rec["ls_m"], now),
            "pos": rec.get("pos"),
            "tx": rec["tx"], "c": rec["c"], "f": rec["f"], "mfg": rec["mfg"], "svc": rec["svc"], "su": rec["su"],
            "s": {src: {"r": v["r"], "avg": round(v["avg"], 1), "ts": self._wall(v["ts"], now)}
                  for src, v in rec["s"].items()},
        }

    def snapshot(self) -> dict:
        now = time.monotonic()
        return {
            "full": True, "running": self.running, "error": self.error,
            "scanners": list(self._scanners.values()),
            "devices": [self._payload(r, now) for r in self._devs.values()],
            "expire_s": EXPIRE_S,
            "kinds": {k: {"l": v[0], "icon": v[1]} for k, v in KINDS.items()},
        }

    def status(self) -> dict:
        return {"running": self.running, "error": self.error, "devices": len(self._devs),
                "scanners": len(self._scanners)}

    @callback
    def async_subscribe(self, cb: Callable[[dict], None]) -> Callable[[], None]:
        self._subs.add(cb)

        @callback
        def unsub() -> None:
            self._subs.discard(cb)

        return unsub
