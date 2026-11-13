"""WebSocket-API von Home Twin."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from . import ble_proxies
from .ble_tags import KINDS
from .const import DEFAULT_PLAN_ID, DOMAIN, VERSION
from .db import ConflictError
from .manager import TwinManager, TwinUnavailable

TAG_ADDRESS = vol.All(str, vol.Match(r"^([0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}|IRK-[0-9A-Fa-f]{12})$"))
PLAN_ID = vol.Optional("plan_id", default=DEFAULT_PLAN_ID)


def _mgr(hass: HomeAssistant) -> TwinManager:
    return hass.data[DOMAIN]


@callback
def async_register(hass: HomeAssistant) -> None:
    for cmd in (ws_status, ws_get, ws_subscribe, ws_save, ws_revisions, ws_revision, ws_import, ws_ble_subscribe,
                ws_proxy_list, ws_proxy_create, ws_tag_set, ws_tag_image, ws_tag_remove, ws_tag_set_irk, ws_tag_list,
                ws_tag_assign, ws_persons):
        websocket_api.async_register_command(hass, cmd)


@websocket_api.websocket_command({
    vol.Required("type"): "home_twin/ble/tag/set",
    vol.Required("address"): TAG_ADDRESS,
    vol.Required("name"): vol.All(str, vol.Length(min=1, max=100)),
    vol.Optional("kind"): vol.In(list(KINDS)),
    vol.Optional("icon"): vol.Any(None, vol.All(str, vol.Match(r"^mdi:[a-z0-9-]+$"))),
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_tag_set(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """BLE-Tag hinzufügen oder ändern -> Gerät + Sensoren in HA."""
    args = (msg["icon"],) if "icon" in msg else ()
    tag = await _mgr(hass).tags.async_set(msg["address"], msg["name"], msg.get("kind"), *args)
    connection.send_result(msg["id"], tag)


@websocket_api.websocket_command({
    vol.Required("type"): "home_twin/ble/tag/image",
    vol.Required("address"): TAG_ADDRESS,
    vol.Required("data"): vol.Any(None, vol.All(str, vol.Length(max=800_000))),
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_tag_image(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Eigenes Bild für einen BLE-Tag (data:-URL) setzen oder mit data=null entfernen."""
    try:
        tag = await _mgr(hass).tags.async_set_image(msg["address"], msg["data"])
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_image", str(err))
        return
    connection.send_result(msg["id"], tag)


@websocket_api.websocket_command({
    vol.Required("type"): "home_twin/ble/tag/set_irk",
    vol.Required("irk"): str,
    vol.Required("name"): vol.All(str, vol.Length(min=1, max=100)),
    vol.Optional("kind"): vol.In(list(KINDS)),
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_tag_set_irk(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """iPhone/Watch/Android per IRK hinzufügen – wird unter jeder wechselnden Adresse wiedererkannt."""
    try:
        tag = await _mgr(hass).tags.async_set_irk(msg["irk"], msg["name"], msg.get("kind"))
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_irk", str(err))
        return
    connection.send_result(msg["id"], tag)


@websocket_api.websocket_command({vol.Required("type"): "home_twin/ble/tag/list"})
@callback
def ws_tag_list(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Hinzugefügte BLE-Geräte (ohne IRK-Werte)."""
    connection.send_result(msg["id"], _mgr(hass).tags.public_list())


@websocket_api.websocket_command({vol.Required("type"): "home_twin/ble/tag/remove", vol.Required("address"): str})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_tag_remove(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """BLE-Tag entfernen -> Gerät und Sensoren in HA verschwinden."""
    await _mgr(hass).tags.async_remove(msg["address"])
    connection.send_result(msg["id"])


@websocket_api.websocket_command({vol.Required("type"): "home_twin/ble/proxy/list"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_proxy_list(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Proxy-Dateien in /config/esphome + Räume (enthält API-Schlüssel, daher nur Admin)."""
    items = await hass.async_add_executor_job(ble_proxies.list_proxies, hass)
    connection.send_result(msg["id"], {"proxies": items, "rooms": ble_proxies.ROOMS, "max_per_room": ble_proxies.MAX_PER_ROOM})


@websocket_api.websocket_command({vol.Required("type"): "home_twin/ble/proxy/create", vol.Required("room"): str})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_proxy_create(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Neuen Proxy im Raum anlegen: nächste freie Nummer, eigene Schlüssel, Datei für ESPHome."""
    try:
        res = await hass.async_add_executor_job(ble_proxies.create_proxy, hass, msg["room"])
    except (ValueError, FileExistsError, OSError) as err:
        connection.send_error(msg["id"], "proxy_create_failed", str(err))
        return
    connection.send_result(msg["id"], res)


@websocket_api.websocket_command({vol.Required("type"): "home_twin/ble/subscribe"})
@callback
def ws_ble_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Live-Strom aller BLE-Geräte: erst {"full": true, ...}, danach je Sekunde {"upd", "del", "scanners"?}."""
    trk = _mgr(hass).ble

    @callback
    def forward(data: dict) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], data))

    connection.subscriptions[msg["id"]] = trk.async_subscribe(forward)
    connection.send_result(msg["id"])
    forward(trk.snapshot())


@websocket_api.websocket_command({vol.Required("type"): "home_twin/status"})
@callback
def ws_status(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    mgr = _mgr(hass)
    connection.send_result(msg["id"], {"version": VERSION, **mgr.status(), "ble": mgr.ble.status()})


@websocket_api.websocket_command({vol.Required("type"): "home_twin/plan/get", PLAN_ID: str})
@callback
def ws_get(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    connection.send_result(msg["id"], _mgr(hass).snapshot(msg["plan_id"]))


@websocket_api.websocket_command({vol.Required("type"): "home_twin/plan/subscribe", PLAN_ID: str})
@callback
def ws_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    mgr = _mgr(hass)

    @callback
    def forward(snap: dict) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], snap))

    connection.subscriptions[msg["id"]] = mgr.async_subscribe(msg["plan_id"], forward)
    connection.send_result(msg["id"])
    forward(mgr.snapshot(msg["plan_id"]))


@websocket_api.websocket_command({
    vol.Required("type"): "home_twin/plan/save",
    PLAN_ID: str,
    vol.Required("plan"): dict,
    vol.Optional("base_revision"): vol.Any(int, None),
    vol.Optional("force", default=False): bool,
    vol.Optional("note"): str,
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_save(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    user = connection.user
    try:
        res = await _mgr(hass).async_save(
            msg["plan_id"], msg["plan"], msg.get("base_revision"), msg["force"],
            user_id=user.id if user else None, user_name=user.name if user else None, note=msg.get("note"),
        )
    except ConflictError as err:
        connection.send_error(msg["id"], "conflict", str(err))
        return
    except TwinUnavailable as err:
        connection.send_error(msg["id"], "db_unavailable", f"Datenbank nicht erreichbar: {err}")
        return
    except ValueError as err:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, str(err))
        return
    connection.send_result(msg["id"], res)


@websocket_api.websocket_command({
    vol.Required("type"): "home_twin/revisions/list",
    PLAN_ID: str,
    vol.Optional("limit", default=50): vol.All(int, vol.Range(min=1, max=1000)),
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_revisions(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    try:
        res = await _mgr(hass).async_list_revisions(msg["plan_id"], msg["limit"])
    except TwinUnavailable as err:
        connection.send_error(msg["id"], "db_unavailable", str(err))
        return
    connection.send_result(msg["id"], res)


@websocket_api.websocket_command({
    vol.Required("type"): "home_twin/revisions/get",
    PLAN_ID: str,
    vol.Required("revision"): int,
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_revision(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    try:
        res = await _mgr(hass).async_get_revision(msg["plan_id"], msg["revision"])
    except TwinUnavailable as err:
        connection.send_error(msg["id"], "db_unavailable", str(err))
        return
    if res is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Revision nicht gefunden")
        return
    connection.send_result(msg["id"], res)


@websocket_api.websocket_command({
    vol.Required("type"): "home_twin/plan/import_lovelace",
    PLAN_ID: str,
    vol.Required("dashboard"): str,
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_import(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    user = connection.user
    try:
        res = await _mgr(hass).async_import_lovelace(
            msg["plan_id"], msg["dashboard"], user_id=user.id if user else None, user_name=user.name if user else None
        )
    except TwinUnavailable as err:
        connection.send_error(msg["id"], "db_unavailable", str(err))
        return
    except HomeAssistantError as err:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, str(err))
        return
    connection.send_result(msg["id"], res)


@websocket_api.websocket_command({
    vol.Required("type"): "home_twin/ble/tag/assign",
    vol.Required("address"): TAG_ADDRESS,
    vol.Optional("person"): vol.Any(None, vol.All(str, vol.Match(r"^person\.[a-z0-9_]+$"))),
    vol.Optional("presence"): bool,
    vol.Optional("locate"): bool,
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_tag_assign(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """BLE-Gerät einer Person zuordnen (person=null hebt auf); presence/locate je Gerät."""
    kw = {k: msg[k] for k in ("person", "presence", "locate") if k in msg}
    try:
        tag = await _mgr(hass).tags.async_assign(msg["address"], **kw)
    except ValueError as err:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, str(err))
        return
    connection.send_result(msg["id"], tag)


@websocket_api.websocket_command({vol.Required("type"): "home_twin/ble/persons"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_persons(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Personen + HA-Benutzer mit ihren BLE-Geräten und dem Live-Stand (Anwesenheit, Raum)."""
    mgr = _mgr(hass)
    ent_reg = er.async_get(hass)
    users = [u for u in await hass.auth.async_get_users() if not u.system_generated and u.is_active]
    persons = []
    for st in hass.states.async_all("person"):
        slug = st.entity_id.split(".", 1)[1]
        reg_e = ent_reg.async_get(st.entity_id)
        persons.append({
            "entity_id": st.entity_id, "person_id": reg_e.unique_id if reg_e else None,
            "name": st.name, "state": st.state,
            "user_id": st.attributes.get("user_id"), "picture": st.attributes.get("entity_picture"),
            "device_trackers": st.attributes.get("device_trackers") or [],
            "ble_tracker": ent_reg.async_get_entity_id("device_tracker", DOMAIN, f"person_{slug}_tracker"),
            "ble_room": ent_reg.async_get_entity_id("sensor", DOMAIN, f"person_{slug}_room"),
            "live": mgr.persons.public().get(st.entity_id),
        })
    tags = mgr.tags.public_list()
    now = None
    for t in tags:
        info = mgr.ble.tag_info(t["address"], now)
        t.update({k: info[k] for k in ("room", "proxy", "rssi", "age")})
        t["tracker"] = ent_reg.async_get_entity_id("device_tracker", DOMAIN, f"ble_{t['address']}_tracker")
    connection.send_result(msg["id"], {
        "persons": persons,
        "users": [{"id": u.id, "name": u.name, "admin": u.is_admin, "owner": u.is_owner,
                   "local_only": u.local_only} for u in users],
        "tags": tags,
    })
