"""Gemeinsame Basis für die Entitäten hinzugefügter BLE-Tags."""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar, device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .ble_tags import KINDS, SIGNAL_UPD
from .const import DOMAIN

if TYPE_CHECKING:
    from .manager import TwinManager

AVAILABLE_S = 600   # so lange nach dem letzten Empfang gelten Werte noch
HOME_S = 120        # Device-Tracker "zuhause", wenn so kürzlich empfangen


def tag_device_id(addr: str) -> tuple[str, str]:
    return (DOMAIN, f"ble_{addr}")


def _proxy_device(hass: HomeAssistant, src: str) -> dr.DeviceEntry | None:
    reg = dr.async_get(hass)
    return (reg.async_get_device(connections={(dr.CONNECTION_BLUETOOTH, src)})
            or reg.async_get_device(connections={(dr.CONNECTION_NETWORK_MAC, src.lower())}))


def proxy_name(hass: HomeAssistant, mgr: TwinManager, src: str) -> str:
    """Anzeigename des Proxys wie in HA (z. B. "FL-01 BLE-Proxy"), sonst Scanner-Name ohne MAC."""
    # Die Bluetooth-Integration legt je Scanner ein Gerät "<Name> (<MAC>)" an – MAC weglassen
    dev = _proxy_device(hass, src)
    name = (dev.name_by_user or dev.name) if dev else None
    return re.sub(r"\s*\([0-9A-Fa-f:]{17}\)\s*$", "", name or mgr.ble.scanner_name(src))


def proxy_area(hass: HomeAssistant, mgr: TwinManager, src: str) -> str:
    """Bereich (Raum) des Proxys, der den Tag am stärksten hört – sonst sein Name."""
    dev = _proxy_device(hass, src)
    if dev and dev.area_id:
        area = ar.async_get(hass).async_get_area(dev.area_id)
        if area:
            return area.name
    return proxy_name(hass, mgr, src)


class TagEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, mgr: TwinManager, addr: str, key: str) -> None:
        self.mgr = mgr
        self.addr = addr
        self._attr_unique_id = f"ble_{addr}_{key}"
        tag = mgr.tags.tags.get(addr, {})
        rec = mgr.ble.rec(addr) or {}
        self._attr_device_info = DeviceInfo(
            identifiers={tag_device_id(addr)},
            name=tag.get("name") or addr,
            manufacturer="Home Twin BLE",
            model=rec.get("t") or KINDS.get(tag.get("kind") or "", ("BLE-Tag",))[0],
            serial_number=addr,
        )

    def _seen_within(self, seconds: float) -> bool:
        ls = self.mgr.ble.last_seen(self.addr)
        return ls is not None and time.time() - ls < seconds

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_UPD.format(self.addr), self._on_update))
        self._attr_available = self._seen_within(AVAILABLE_S)
        self._refresh()

    def _refresh(self) -> bool:
        """Werte neu berechnen; True, wenn sich etwas geändert hat."""
        return True

    @callback
    def _on_update(self) -> None:
        avail = self._seen_within(AVAILABLE_S)
        changed = self._refresh()
        if changed or avail != self._attr_available:
            self._attr_available = avail
            self.async_write_ha_state()


class PersonEntity(Entity):
    """Basis für die Entitäten einer Person (aus ihren BLE-Geräten berechnet)."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, mgr: TwinManager, person: str, key: str, domain: str, suffix: str = "") -> None:
        from .persons import person_slug

        self.mgr = mgr
        self.person = person
        slug = person_slug(person)
        self._attr_unique_id = f"person_{slug}_{key}"
        # Namensschema <raum>_<gewerk>_<funktion>[_<objekt>]: haus_sys_ble_<person>[_room]
        self.entity_id = f"{domain}.haus_sys_ble_{slug}{suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={person_device_id(person)},
            name=f"{mgr.persons.person_name(person)} BLE",
            manufacturer="Home Twin BLE",
            model="Person (BLE-Ortung)",
        )

    @property
    def pstate(self) -> dict:
        return self.mgr.persons.state.get(self.person) or {}

    async def async_added_to_hass(self) -> None:
        from .persons import SIGNAL_PERSON

        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_PERSON.format(self.person), self._on_update))
        self._refresh()

    def _refresh(self) -> None:
        """Werte aus pstate übernehmen."""

    @callback
    def _on_update(self) -> None:
        self._refresh()
        self.async_write_ha_state()


def person_device_id(person: str) -> tuple[str, str]:
    return (DOMAIN, f"person_{person}")
