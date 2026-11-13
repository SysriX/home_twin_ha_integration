"""BLE-Geräte → Personen: Anwesenheit und Ort im Haus je Person.

Jedes hinzugefügte BLE-Gerät (Tag, Schlüsselfinder, iPhone per IRK …) kann einer
HA-Person (person.*) zugeordnet werden. Je Gerät zwei Schalter:
  - presence: zählt für die Anwesenheit (zuhause, solange kürzlich empfangen)
  - locate:   bestimmt den Raum/Ort der Person (stärkstes frisches Signal)

Für jede Person mit mindestens einem Gerät entstehen in HA:
  device_tracker.haus_sys_ble_<person>      zuhause/nicht zuhause, Attribute Raum/Etage/x/y
  sensor.haus_sys_ble_<person>_room         Raum, in dem die Person gerade ist
Der Tracker lässt sich in der Person als Gerät eintragen (macht die Karte), dann
zählt BLE für zone.home / binary_sensor.haus_sys_presence.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

if TYPE_CHECKING:
    from .manager import TwinManager

SIGNAL_PERSON = "home_twin_person_{}"   # Stand einer Person hat sich geändert
TICK = timedelta(seconds=3)
HOME_S = 120         # zuhause, wenn ein Anwesenheits-Gerät so kürzlich empfangen wurde
LOC_S = 45           # Ort nur aus Geräten, die so frisch sind
SWITCH_DB = 4.0      # anderes Gerät bestimmt den Ort erst, wenn es so viel stärker ist


def person_slug(person: str) -> str:
    return person.split(".", 1)[-1]


class PersonPresence:
    """Berechnet je Person Anwesenheit + Ort aus ihren BLE-Geräten."""

    def __init__(self, hass: HomeAssistant, mgr: TwinManager) -> None:
        self.hass = hass
        self.mgr = mgr
        self.state: dict[str, dict[str, Any]] = {}
        self._src: dict[str, str] = {}       # Person -> Gerät, das gerade den Ort bestimmt
        self._unsub: Callable[[], None] | None = None

    @callback
    def async_start(self) -> None:
        if self._unsub is None:
            self._unsub = async_track_time_interval(self.hass, self._tick, TICK, name="home_twin persons")
        self._tick()

    @callback
    def async_stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    def person_name(self, person: str) -> str:
        st = self.hass.states.get(person)
        return (st.name if st else None) or person_slug(person).replace("_", " ").title()

    def compute(self, person: str, now: float | None = None) -> dict[str, Any]:
        tags = self.mgr.tags
        ble = self.mgr.ble
        now = time.monotonic() if now is None else now
        infos = []
        for a in tags.of_person(person):
            pres, loc = tags.flags(a)
            infos.append({**ble.tag_info(a, now), "presence": pres, "locate": loc})
        home_by = [i for i in infos if i["presence"] and i["age"] is not None and i["age"] < HOME_S]
        fresh = [i for i in infos if i["locate"] and i["age"] is not None and i["age"] < LOC_S
                 and i["rssi"] is not None and i["room"]]
        cur = next((i for i in fresh if i["key"] == self._src.get(person)), None)
        best = max(fresh, key=lambda i: i["rssi"], default=None)
        if cur is not None and best is not None and best["rssi"] - cur["rssi"] < SWITCH_DB:
            best = cur
        if best is not None:
            self._src[person] = best["key"]
        else:
            self._src.pop(person, None)
        return {
            "person": person,
            "name": self.person_name(person),
            "home": bool(home_by),
            "room": best["room"] if best else None,
            "floor": best["floor"] if best else None,
            "x": best["x"] if best else None,
            "y": best["y"] if best else None,
            "tag": best["key"] if best else None,
            "tag_name": best["name"] if best else None,
            "proxy": best["proxy"] if best else None,
            "rssi": best["rssi"] if best else None,
            "seen_by": [i["name"] for i in home_by],
            "last_seen_s": min((i["age"] for i in infos if i["age"] is not None), default=None),
            "tags": infos,
        }

    @callback
    def _tick(self, _now: Any = None) -> None:
        if self.mgr.tags is None or self.mgr.ble is None:
            return
        now = time.monotonic()
        persons = self.mgr.tags.persons()
        for p in list(self.state):
            if p not in persons:
                del self.state[p]
                self._src.pop(p, None)
        for p in persons:
            new = self.compute(p, now)
            old = self.state.get(p)
            self.state[p] = new
            if old is None or self._sig(old) != self._sig(new):
                async_dispatcher_send(self.hass, SIGNAL_PERSON.format(p))

    @staticmethod
    def _sig(st: dict) -> tuple:
        """Was die Entitäten zeigen (Alter/Signal grob, damit nicht jede Sekunde geschrieben wird)."""
        return (st["home"], st["room"], st["floor"], st["tag"], st["name"],
                None if st["x"] is None else round(st["x"], 1), None if st["y"] is None else round(st["y"], 1),
                tuple(st["seen_by"]), None if st["rssi"] is None else round(st["rssi"] / 3))

    def public(self) -> dict[str, dict]:
        """Für die Karte (ohne Wiederholung der Tag-Liste)."""
        return {p: {k: v for k, v in st.items() if k != "tags"} for p, st in self.state.items()}
