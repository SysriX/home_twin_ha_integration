"""Anwesenheit hinzugefügter BLE-Tags (zuhause = von einem Proxy kürzlich empfangen)."""

from __future__ import annotations

from homeassistant.components.device_tracker import BaseScannerEntity, SourceType
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .ble_entity import HOME_S, PersonEntity, TagEntity
from .ble_tags import SIGNAL_TAGS
from .const import DOMAIN

# Identitäts-Werte, die als Attribute am Tracker stehen (zum Zuordnen in Automationen)
ATTR_KEYS = ("uuid", "major", "minor", "namespace", "instance", "url", "model", "firmware", "mac")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    mgr = hass.data[DOMAIN]
    known: set[str] = set()

    known_p: set[str] = set()

    @callback
    def sync() -> None:
        new = [TagTracker(mgr, a) for a in mgr.tags.tags if a not in known]
        known.update(e.addr for e in new)
        known.intersection_update(mgr.tags.tags)
        persons = mgr.tags.persons()
        newp = [PersonTracker(mgr, p) for p in persons if p not in known_p]
        known_p.update(e.person for e in newp)
        known_p.intersection_update(persons)   # Gerät der Person verschwindet (siehe __init__)
        if new or newp:
            add([*new, *newp])

    sync()
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_TAGS, sync))


class TagTracker(TagEntity, BaseScannerEntity):
    _attr_name = None            # heißt wie das Gerät (Tag-Name)
    _attr_source_type = SourceType.BLUETOOTH_LE

    def __init__(self, mgr, addr: str) -> None:
        super().__init__(mgr, addr, "tracker")
        self._attr_icon = mgr.tags.icon(addr)
        self._attr_entity_picture = mgr.tags.image(addr)
        self._home = False
        self._attrs: dict = {}

    @property
    def available(self) -> bool:
        return True              # "nicht zuhause" statt "nicht verfügbar"

    @property
    def is_connected(self) -> bool:
        return self._home

    @property
    def extra_state_attributes(self) -> dict:
        return self._attrs

    def _refresh(self) -> bool:
        home = self._seen_within(HOME_S)
        vals = self.mgr.ble.values(self.addr)
        rec = self.mgr.ble.rec(self.addr) or {}
        attrs = {"address": rec.get("ra") or self.addr, **{k: vals[k]["v"] for k in ATTR_KEYS if k in vals}}
        if self.addr.startswith("IRK-"):
            attrs["erkannt_per"] = "IRK"
        icon, pic = self.mgr.tags.icon(self.addr), self.mgr.tags.image(self.addr)
        if (home, attrs, icon, pic) == (self._home, self._attrs, self._attr_icon, self._attr_entity_picture):
            return False
        self._home, self._attrs, self._attr_icon, self._attr_entity_picture = home, attrs, icon, pic
        return True


class PersonTracker(PersonEntity, BaseScannerEntity):
    """Person zuhause, solange eines ihrer Anwesenheits-Geräte empfangen wird – mit Raum/Ort."""

    _attr_name = None            # heißt wie das Gerät ("<Person> BLE")
    _attr_source_type = SourceType.BLUETOOTH_LE
    _attr_icon = "mdi:account-search"

    def __init__(self, mgr, person: str) -> None:
        super().__init__(mgr, person, "tracker", "device_tracker")
        self._home = False
        self._attrs: dict = {}

    @property
    def available(self) -> bool:
        return True

    @property
    def is_connected(self) -> bool:
        return self._home

    @property
    def extra_state_attributes(self) -> dict:
        return self._attrs

    def _refresh(self) -> None:
        st = self.pstate
        self._home = bool(st.get("home"))
        self._attrs = {
            "person": self.person,
            "room": st.get("room"), "floor": st.get("floor"), "x": st.get("x"), "y": st.get("y"),
            "tag": st.get("tag_name"), "proxy": st.get("proxy"), "rssi": st.get("rssi"),
            "seen_by": st.get("seen_by") or [],
            "devices": [t["name"] for t in st.get("tags") or []],
        }
