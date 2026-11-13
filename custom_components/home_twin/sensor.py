"""Sensoren hinzugefügter BLE-Tags.

Feste Sensoren: Raum (Bereich des nächsten Proxys), nächster Proxy, Signal, zuletzt gesehen.
Dazu je ein Sensor für jeden Wert, den der Tag tatsächlich sendet (Batterie, Temperatur,
Beschleunigung, Taste …) – sie entstehen automatisch, sobald der Wert zum ersten Mal kommt.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONCENTRATION_PARTS_PER_MILLION,
    LIGHT_LUX,
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .ble_entity import PersonEntity, TagEntity, proxy_area, proxy_name
from .ble_tags import SIGNAL_TAGS
from .const import DOMAIN

DIAG = EntityCategory.DIAGNOSTIC
M = SensorStateClass.MEASUREMENT

# Wert-Schlüssel aus ble_decode -> (Device-Class, Einheit, State-Class, Kategorie, standardmäßig aktiv, Icon)
FIELDS: dict[str, tuple] = {
    "battery_pct": (SensorDeviceClass.BATTERY, PERCENTAGE, M, None, True, None),
    "battery_mv": (SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.MILLIVOLT, M, DIAG, True, None),
    "temperature": (SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS, M, None, True, None),
    "humidity": (SensorDeviceClass.HUMIDITY, PERCENTAGE, M, None, True, None),
    "cpu_temp": (SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS, M, DIAG, False, None),
    "acc_x": (None, "mg", M, None, True, "mdi:axis-x-arrow"),
    "acc_y": (None, "mg", M, None, True, "mdi:axis-y-arrow"),
    "acc_z": (None, "mg", M, None, True, "mdi:axis-z-arrow"),
    "alarm": (None, None, M, None, True, "mdi:gesture-tap-button"),
    "pir": (None, None, M, None, True, "mdi:motion-sensor"),
    "lux": (SensorDeviceClass.ILLUMINANCE, LIGHT_LUX, M, None, True, None),
    "co2": (SensorDeviceClass.CO2, CONCENTRATION_PARTS_PER_MILLION, M, None, True, None),
    "voc": (None, None, M, None, True, "mdi:air-filter"),
    "nox": (None, None, M, None, True, "mdi:air-filter"),
    "records": (None, None, M, DIAG, False, "mdi:database"),
    "adv_count": (None, None, SensorStateClass.TOTAL_INCREASING, DIAG, False, "mdi:counter"),
    "uptime_s": (SensorDeviceClass.DURATION, UnitOfTime.SECONDS, None, DIAG, False, None),
}
# feste Namen, wo der Decoder-Text doppelt wäre (Batterie in % und mV)
NAMES = {"battery_pct": "Batterie", "battery_mv": "Batteriespannung"}
FIXED = ("raum", "proxy", "rssi", "last_seen")
ROOM_HYST_DB = 3.0   # Raumwechsel erst, wenn ein anderer Proxy so viel stärker ist


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    mgr = hass.data[DOMAIN]
    known: dict[str, set[str]] = {}
    known_p: set[str] = set()

    @callback
    def sync(_now=None) -> None:
        new: list[SensorEntity] = []
        for addr in list(mgr.tags.tags):
            keys = known.setdefault(addr, set())
            for k in FIXED:
                if k not in keys:
                    keys.add(k)
                    new.append(FixedSensor(mgr, addr, k))
            for k in mgr.ble.values(addr):
                if k in FIELDS and k not in keys:
                    keys.add(k)
                    new.append(ValueSensor(mgr, addr, k))
        for addr in [a for a in known if a not in mgr.tags.tags]:
            del known[addr]   # Entitäten verschwinden mit dem Gerät (siehe __init__)
        persons = mgr.tags.persons()
        for p in persons - known_p:
            known_p.add(p)
            new.append(PersonRoomSensor(mgr, p))
        known_p.intersection_update(persons)
        if new:
            add(new)

    sync()
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_TAGS, sync))
    # neue Werte (z. B. erster TLM-Frame) -> neue Sensoren
    entry.async_on_unload(async_track_time_interval(hass, sync, timedelta(seconds=10)))


class ValueSensor(TagEntity, SensorEntity):
    def __init__(self, mgr, addr: str, key: str) -> None:
        super().__init__(mgr, addr, key)
        self.key = key
        dc, unit, sc, cat, enabled, icon = FIELDS[key]
        v = mgr.ble.values(addr).get(key) or {}
        self._attr_name = NAMES.get(key) or v.get("l") or key
        self._attr_device_class = dc
        self._attr_native_unit_of_measurement = unit
        self._attr_state_class = sc
        self._attr_entity_category = cat
        self._attr_entity_registry_enabled_default = enabled
        if icon:
            self._attr_icon = icon

    def _refresh(self) -> bool:
        v = self.mgr.ble.values(self.addr).get(self.key)
        if v is None:
            return False
        val, attrs = v["v"], {"frame": v["frame"]}
        if (val, attrs) == (self._attr_native_value, getattr(self, "_attr_extra_state_attributes", None)):
            return False
        self._attr_native_value = val
        self._attr_extra_state_attributes = attrs
        return True


class FixedSensor(TagEntity, SensorEntity):
    NAMES = {"raum": "Raum", "proxy": "Nächster Proxy", "rssi": "Signal", "last_seen": "Zuletzt gesehen"}

    def __init__(self, mgr, addr: str, key: str) -> None:
        super().__init__(mgr, addr, key)
        self.key = key
        self._attr_name = self.NAMES[key]
        self._src: str | None = None
        if key == "raum":
            self._attr_icon = "mdi:home-map-marker"
        elif key == "proxy":
            self._attr_icon = "mdi:access-point"
            self._attr_entity_category = DIAG
        elif key == "rssi":
            self._attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
            self._attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
            self._attr_state_class = M
            self._attr_entity_category = DIAG
            self._attr_entity_registry_enabled_default = False
        else:
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
            self._attr_entity_category = DIAG

    def _nearest(self) -> str | None:
        """Stärksten Proxy mit Hysterese, damit der Raum nicht hin- und herspringt."""
        rec = self.mgr.ble.rec(self.addr)
        if not rec or not rec["s"]:
            return None
        best_src, best = max(rec["s"].items(), key=lambda kv: kv[1]["avg"])
        cur = rec["s"].get(self._src) if self._src else None
        if cur is not None and best["avg"] - cur["avg"] < ROOM_HYST_DB:
            return self._src
        self._src = best_src
        return best_src

    def _refresh(self) -> bool:
        if self.key == "last_seen":
            ls = self.mgr.ble.last_seen(self.addr)
            # auf die Minute gerundet – sonst schreibt der Sensor jede Sekunde
            val = datetime.fromtimestamp(int(ls // 60 * 60), timezone.utc) if ls else None
        elif self.key == "rssi":
            b = self.mgr.ble.best(self.addr)
            val = round(b["avg"]) if b else None
        else:
            src = self._nearest()
            if src is None:
                val = None
            elif self.key == "proxy":
                val = proxy_name(self.hass, self.mgr, src)
            else:
                pos = (self.mgr.ble.rec(self.addr) or {}).get("pos")
                val = pos["rn"] if pos and pos.get("rn") else proxy_area(self.hass, self.mgr, src)
        if val == self._attr_native_value:
            return False
        self._attr_native_value = val
        return True


class PersonRoomSensor(PersonEntity, SensorEntity):
    """Raum, in dem die Person gerade ist (aus ihrem am stärksten empfangenen Ortungs-Gerät)."""

    _attr_name = "Raum"
    _attr_icon = "mdi:account-box-outline"

    def __init__(self, mgr, person: str) -> None:
        super().__init__(mgr, person, "room", "sensor", "_room")

    def _refresh(self) -> None:
        st = self.pstate
        self._attr_native_value = st.get("room")
        self._attr_extra_state_attributes = {
            "person": self.person, "floor": st.get("floor"), "x": st.get("x"), "y": st.get("y"),
            "tag": st.get("tag_name"), "proxy": st.get("proxy"), "rssi": st.get("rssi"),
            "home": bool(st.get("home")),
        }
