"""Home Twin – digitales Abbild des Hauses (Grundriss, Geräte, BLE-Ortung).

Konfiguration (configuration.yaml):

    home_twin:
      db_url: !secret home_twin_db_url

Der Integrationseintrag "Home Twin" wird beim Start automatisch angelegt; über ihn
bekommen hinzugefügte BLE-Tags ihre Geräte und Sensoren.
"""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.typing import ConfigType

from . import websocket
from .ble import BleTracker
from .ble_tags import SIGNAL_TAGS, TagStore
from .const import (
    CONF_DB_URL,
    CONF_IMPORT_DASHBOARD,
    CONF_KEEP_REVISIONS,
    DEFAULT_IMPORT_DASHBOARD,
    DEFAULT_KEEP_REVISIONS,
    DOMAIN,
)
from .manager import TwinManager
from .persons import PersonPresence

PLATFORMS = [Platform.DEVICE_TRACKER, Platform.SENSOR]

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema({
            vol.Required(CONF_DB_URL): cv.string,
            vol.Optional(CONF_IMPORT_DASHBOARD, default=DEFAULT_IMPORT_DASHBOARD): cv.string,
            vol.Optional(CONF_KEEP_REVISIONS, default=DEFAULT_KEEP_REVISIONS): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=10000)
            ),
        })
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    mgr = TwinManager(hass, config[DOMAIN])
    mgr.ble = BleTracker(hass)
    mgr.tags = TagStore(hass, mgr)
    mgr.ble.tagstore = mgr.tags
    mgr.ble.mgr = mgr
    mgr.persons = PersonPresence(hass, mgr)
    hass.data[DOMAIN] = mgr
    websocket.async_register(hass)
    await mgr.async_start()
    await mgr.tags.async_load()

    # BLE-Erfassung erst nach dem Start (Bluetooth-Scanner/Proxys sind dann verbunden)
    @callback
    def _start_ble(_hass: HomeAssistant) -> None:
        mgr.ble.async_start()
        mgr.persons.async_start()

    async_at_started(hass, _start_ble)

    async def _ble_status(_call: ServiceCall) -> ServiceResponse:
        """Live-Stand aller hinzugefügten BLE-Geräte (für Skripte, z. B. Tür nur bei Nähe öffnen)."""
        return {"tags": mgr.ble.tag_status(), "persons": mgr.persons.public()}

    hass.services.async_register(DOMAIN, "ble_status", _ble_status, supports_response=SupportsResponse.ONLY)

    if not hass.config_entries.async_entries(DOMAIN):
        hass.async_create_task(
            hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_IMPORT}, data={})
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    mgr: TwinManager = hass.data[DOMAIN]

    @callback
    def _tags_changed() -> None:
        """Geräte entfernter Tags löschen, umbenannte nachziehen, Live-Karten aktualisieren."""
        reg = dr.async_get(hass)
        persons = mgr.tags.persons()
        for dev in dr.async_entries_for_config_entry(reg, entry.entry_id):
            addr = next((i[1][4:] for i in dev.identifiers if i[0] == DOMAIN and i[1].startswith("ble_")), None)
            if addr is None:
                person = next((i[1][7:] for i in dev.identifiers if i[0] == DOMAIN and i[1].startswith("person_")), None)
                if person is not None and person not in persons:
                    reg.async_remove_device(dev.id)   # Person hat keine BLE-Geräte mehr
                continue
            tag = mgr.tags.tags.get(addr)
            if tag is None:
                reg.async_remove_device(dev.id)
            elif dev.name != tag.get("name"):
                reg.async_update_device(dev.id, name=tag.get("name"))
            mgr.ble.mark_dirty(addr)
        for addr in mgr.tags.tags:
            mgr.ble.mark_dirty(addr)
        mgr.persons._tick()   # Personen-Stand sofort neu (Zuordnung geändert)

    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_TAGS, _tags_changed))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _tags_changed()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_config_entry_device(hass: HomeAssistant, entry: ConfigEntry, device: dr.DeviceEntry) -> bool:
    """Gerät in der HA-Oberfläche löschen = Tag entfernen."""
    mgr: TwinManager = hass.data[DOMAIN]
    for ident in device.identifiers:
        if ident[0] == DOMAIN and ident[1].startswith("ble_"):
            # erst nachdem HA das Gerät gelöscht hat, sonst löscht _tags_changed es doppelt
            hass.async_create_task(mgr.tags.async_remove(ident[1][4:]))
        elif ident[0] == DOMAIN and ident[1].startswith("person_"):
            # Personen-Gerät löschen = alle Zuordnungen dieser Person aufheben
            for a in mgr.tags.of_person(ident[1][7:]):
                hass.async_create_task(mgr.tags.async_assign(a, person=None))
    return True

