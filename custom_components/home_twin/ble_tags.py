"""Hinzugefügte BLE-Tags: Speicher (lokal + MariaDB) und Signale an die Entitäten.

Lokale Kopie (.storage/home_twin.ble_tags) ist sofort gültig, die Datenbank wird
gespiegelt. Ist sie gerade nicht erreichbar, merkt sich der Speicher die offenen
Änderungen und gleicht beim nächsten Takt ab.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import time
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

if TYPE_CHECKING:
    from .manager import TwinManager

_LOGGER = logging.getLogger(__name__)

STORE_KEY = "home_twin.ble_tags"
SIGNAL_TAGS = "home_twin_ble_tags"            # Tag hinzugefügt/geändert/entfernt
SIGNAL_UPD = "home_twin_ble_upd_{}"           # neue Daten für eine Adresse

IMAGE_DIR = ("www", "home_twin", "ble")      # unter /config -> /local/home_twin/ble/...
IMAGE_URL = "/local/home_twin/ble/{}?v={}"
IMAGE_MAX = 512 * 1024                        # Bytes nach dem Dekodieren (Karte verkleinert vorher)
_DATA_URL = re.compile(r"^data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=\s]+)$")
_KEEP = object()                              # Wert unverändert lassen

KINDS = {
    "schluessel": ("Schlüssel", "mdi:key-variant"),
    "person": ("Person", "mdi:account"),
    "tasche": ("Tasche", "mdi:bag-personal"),
    "haustier": ("Haustier", "mdi:paw"),
    "fahrzeug": ("Fahrzeug", "mdi:car"),
    "gegenstand": ("Gegenstand", "mdi:tag-outline"),
}


class TagStore:
    def __init__(self, hass: HomeAssistant, mgr: TwinManager) -> None:
        self.hass = hass
        self.mgr = mgr
        self.store: Store = Store(hass, 1, STORE_KEY)
        self.tags: dict[str, dict] = {}
        self._dirty: set[str] = set()     # noch nicht in der DB
        self._deleted: set[str] = set()   # noch nicht aus der DB gelöscht

    async def async_load(self) -> None:
        data = await self.store.async_load() or {}
        self.tags = data.get("tags") or {}
        self._dirty = set(data.get("dirty") or [])
        self._deleted = set(data.get("deleted") or [])
        await self.async_sync_db()
        if getattr(self.mgr, "ble", None) is not None:
            self.mgr.ble.reload_irks()
        async_track_time_interval(self.hass, self._tick, timedelta(seconds=60), name="home_twin ble tags")

    async def _save(self) -> None:
        await self.store.async_save({"tags": self.tags, "dirty": sorted(self._dirty), "deleted": sorted(self._deleted)})

    async def _tick(self, _now: Any = None) -> None:
        if self._dirty or self._deleted:
            await self.async_sync_db()

    async def async_sync_db(self) -> None:
        """Offene Änderungen in die DB, Einträge aus der DB übernehmen, die lokal fehlen."""
        if not self.mgr.db_ok:
            return
        db = self.mgr.db
        try:
            for a in list(self._deleted):
                await self.hass.async_add_executor_job(db.delete_tag, a)
                self._deleted.discard(a)
            for a in list(self._dirty):
                if a in self.tags:
                    await self.hass.async_add_executor_job(db.upsert_tag, a, self.tags[a])
                self._dirty.discard(a)
            remote = await self.hass.async_add_executor_job(db.list_tags)
        except Exception as err:  # noqa: BLE001 – DB weg, später erneut
            _LOGGER.debug("BLE-Tags: DB-Abgleich verschoben (%s)", err)
            await self._save()
            return
        neu = [a for a in remote if a not in self.tags and a not in self._deleted]
        for a in neu:
            self.tags[a] = remote[a]
        await self._save()
        if neu:
            self._changed()

    @callback
    def _changed(self) -> None:
        if getattr(self.mgr, "ble", None) is not None:
            self.mgr.ble.reload_irks()
        async_dispatcher_send(self.hass, SIGNAL_TAGS)

    @staticmethod
    def norm(address: str) -> str:
        return address.strip().upper()

    async def async_set(self, address: str, name: str, kind: str | None, icon: Any = _KEEP) -> dict:
        """icon: "mdi:…" = eigenes Symbol, None = Symbol der Art, weglassen = unverändert."""
        a = self.norm(address)
        old = self.tags.get(a, {})
        self.tags[a] = {**old, "name": name.strip() or a, "kind": kind or old.get("kind") or "gegenstand",
                        "icon": old.get("icon") if icon is _KEEP else (icon or None)}
        self._dirty.add(a)
        self._deleted.discard(a)
        await self._save()
        await self.async_sync_db()
        self._changed()
        return {"address": a, **{k: v for k, v in self.tags[a].items() if k != "irk"}}

    async def async_set_irk(self, irk: str, name: str, kind: str | None) -> dict:
        """Gerät mit wechselnder Adresse (iPhone, Watch, Android) über seinen IRK hinzufügen."""
        irk = irk.strip().lower()
        if irk.startswith("irk:"):
            irk = irk[4:]
        if len(irk) != 32 or any(c not in "0123456789abcdef" for c in irk):
            raise ValueError("IRK muss 32 Hex-Zeichen haben")
        key = f"IRK-{irk[:12].upper()}"
        self.tags[key] = {**self.tags.get(key, {}), "irk": irk}
        return await self.async_set(key, name, kind or "person")

    async def async_assign(self, address: str, person: Any = _KEEP, presence: Any = _KEEP, locate: Any = _KEEP) -> dict:
        """Tag einer Person (person.*) zuordnen; person=None hebt die Zuordnung auf.

        presence: zählt für die Anwesenheit der Person (zuhause, solange kürzlich empfangen)
        locate:   bestimmt Raum/Ort der Person im Haus
        """
        a = self.norm(address)
        t = self.tags.get(a)
        if t is None:
            raise ValueError("Gerät ist nicht hinzugefügt")
        if person is not _KEEP:
            if person:
                t["person"] = person
            else:
                t.pop("person", None)
        if presence is not _KEEP:
            t["presence"] = bool(presence)
        if locate is not _KEEP:
            t["locate"] = bool(locate)
        self._dirty.add(a)
        await self._save()
        await self.async_sync_db()
        self._changed()
        return self.public(a)

    @callback
    def flags(self, a: str) -> tuple[bool, bool]:
        """(zählt für Anwesenheit, bestimmt Ort) – Standard: beides an."""
        t = self.tags.get(a) or {}
        return t.get("presence", True) is not False, t.get("locate", True) is not False

    @callback
    def of_person(self, person: str) -> list[str]:
        return [a for a, t in self.tags.items() if t.get("person") == person]

    @callback
    def persons(self) -> set[str]:
        return {t["person"] for t in self.tags.values() if t.get("person")}

    def public(self, a: str) -> dict:
        t = self.tags.get(a) or {}
        pres, loc = self.flags(a)
        return {"address": a, "name": t.get("name"), "kind": t.get("kind"), "icon": self.icon(a),
                "image": t.get("image"), "irk": bool(t.get("irk")), "person": t.get("person"),
                "presence": pres, "locate": loc}

    def public_list(self) -> list[dict]:
        """Für Karten: ohne IRK, nur ob einer hinterlegt ist."""
        return [self.public(a) for a, _t in sorted(self.tags.items(), key=lambda kv: kv[1].get("name") or "")]

    async def async_remove(self, address: str) -> None:
        a = self.norm(address)
        if self.tags.pop(a, None) is None:
            return
        await self.hass.async_add_executor_job(self._write_image, a, None, None)
        self._dirty.discard(a)
        self._deleted.add(a)
        await self._save()
        await self.async_sync_db()
        self._changed()

    @callback
    def icon(self, a: str) -> str:
        t = self.tags.get(a) or {}
        return t.get("icon") or KINDS.get(t.get("kind") or "", KINDS["gegenstand"])[1]

    # -- eigenes Bild (liegt unter /config/www/home_twin/ble, Pfad im Tag)
    @staticmethod
    def _image_base(a: str) -> str:
        return re.sub(r"[^A-Z0-9-]", "", a.replace(":", ""))

    def _write_image(self, a: str, ext: str | None, raw: bytes | None) -> None:
        d = self.hass.config.path(*IMAGE_DIR)
        base = self._image_base(a)
        for e in ("png", "jpg", "webp"):
            f = os.path.join(d, f"{base}.{e}")
            if e != ext and os.path.exists(f):
                os.remove(f)
        if raw is not None:
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, f"{base}.{ext}"), "wb") as fh:
                fh.write(raw)

    async def async_set_image(self, address: str, data_url: str | None) -> dict:
        """Bild als data:-URL (png/jpeg/webp) speichern, None entfernt es."""
        a = self.norm(address)
        if a not in self.tags:
            raise ValueError("Gerät ist nicht hinzugefügt")
        if data_url:
            m = _DATA_URL.match(data_url)
            if not m:
                raise ValueError("Nur PNG, JPEG oder WebP als data:-URL")
            raw = base64.b64decode(m.group(2))
            if len(raw) > IMAGE_MAX:
                raise ValueError(f"Bild zu groß ({len(raw) // 1024} KB, max. {IMAGE_MAX // 1024} KB)")
            ext = {"jpeg": "jpg"}.get(m.group(1), m.group(1))
            await self.hass.async_add_executor_job(self._write_image, a, ext, raw)
            self.tags[a]["image"] = IMAGE_URL.format(f"{self._image_base(a)}.{ext}", int(time.time()))
        else:
            await self.hass.async_add_executor_job(self._write_image, a, None, None)
            self.tags[a].pop("image", None)
        self._dirty.add(a)
        await self._save()
        await self.async_sync_db()
        self._changed()
        return {"address": a, **{k: v for k, v in self.tags[a].items() if k != "irk"}}

    @callback
    def image(self, a: str) -> str | None:
        return (self.tags.get(a) or {}).get("image")
