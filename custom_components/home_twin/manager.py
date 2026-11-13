"""Plan-Verwaltung: Datenbank, lokale Kopie, Abonnenten, Lovelace-Import."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.util.json import load_json

from .const import (
    CACHE_STORE_KEY,
    CACHE_STORE_VERSION,
    CONF_DB_URL,
    CONF_IMPORT_DASHBOARD,
    CONF_KEEP_REVISIONS,
    DEFAULT_PLAN_ID,
    LEGACY_CARD_TYPES,
    MAX_PLAN_BYTES,
    SYNC_INTERVAL_S,
)
from .db import ConflictError, TwinDB

_LOGGER = logging.getLogger(__package__)


class TwinUnavailable(HomeAssistantError):
    """Datenbank nicht erreichbar – Plan nur lesbar (lokale Kopie)."""


def find_master_plan(cfg: Any) -> dict | None:
    """Erste Haus-Grundriss-Master-Karte (mit plan:) in einer Lovelace-Konfiguration suchen."""
    stack = [cfg]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            if o.get("type") in LEGACY_CARD_TYPES and isinstance(o.get("plan"), dict):
                return o["plan"]
            stack.extend(reversed(list(o.values())))
        elif isinstance(o, list):
            stack.extend(reversed(o))
    return None


def validate_plan(plan: Any) -> dict:
    if not isinstance(plan, dict):
        raise ValueError("Plan muss ein Objekt sein")
    if not isinstance(plan.get("floors", []), list) or not isinstance(plan.get("views", []), list):
        raise ValueError("floors/views müssen Listen sein")
    if len(json.dumps(plan, separators=(",", ":")).encode()) > MAX_PLAN_BYTES:
        raise ValueError("Plan ist größer als 16 MB")
    return plan


class TwinManager:
    """Hält den aktuellen Plan je plan_id und vermittelt zwischen DB, Cache und Clients."""

    def __init__(self, hass: HomeAssistant, conf: dict) -> None:
        self.hass = hass
        self.db = TwinDB(conf[CONF_DB_URL], conf[CONF_KEEP_REVISIONS])
        self.import_dashboard: str = conf[CONF_IMPORT_DASHBOARD]
        self.store: Store = Store(hass, CACHE_STORE_VERSION, CACHE_STORE_KEY)
        self.plans: dict[str, dict] = {}
        self.db_ok = False
        self.db_error: str | None = None
        self.schema: int | None = None
        self._lock = asyncio.Lock()
        self._listeners: dict[str, set[Callable[[dict], None]]] = {}
        self._unsubs: list[Callable[[], None]] = []
        self._warned = False

    # ------------------------------------------------------------ Lebenszyklus
    async def async_start(self) -> None:
        cached = await self.store.async_load() or {}
        self.plans = cached.get("plans") or {}
        await self._async_connect()
        self._unsubs.append(async_track_time_interval(
            self.hass, self._async_tick, timedelta(seconds=SYNC_INTERVAL_S), name="home_twin sync"
        ))
        self._unsubs.append(self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._async_stop))
        self._unsubs.append(async_at_started(self.hass, self._async_after_start))

    async def _async_stop(self, _event: Event | None = None) -> None:
        for u in self._unsubs:
            try:
                u()
            except Exception:  # noqa: BLE001 - bereits abgemeldet
                pass
        self._unsubs.clear()
        await self.hass.async_add_executor_job(self.db.close)

    def _connect_sync(self) -> int:
        self.db.close()
        self.db.connect()
        return self.db.init_schema()

    async def _async_connect(self) -> bool:
        try:
            self.schema = await self.hass.async_add_executor_job(self._connect_sync)
        except Exception as err:  # noqa: BLE001 - jeder Verbindungsfehler führt in den Offline-Modus
            self._set_offline(err)
            return False
        was = self.db_ok
        self.db_ok, self.db_error, self._warned = True, None, False
        _LOGGER.info("Home Twin: Datenbank verbunden (Schema %s)", self.schema)
        try:
            await self._async_sync_from_db()
        except Exception as err:  # noqa: BLE001
            self._set_offline(err)
            return False
        if not was:
            self._notify_all()
        return True

    def _set_offline(self, err: Exception) -> None:
        was = self.db_ok
        self.db_ok, self.db_error = False, f"{type(err).__name__}: {err}".splitlines()[0][:300]
        if not self._warned:
            _LOGGER.warning("Home Twin: Datenbank nicht erreichbar, nutze lokale Kopie (%s)", self.db_error)
            self._warned = True
        if was:
            self._notify_all()

    async def _async_tick(self, _now: Any = None) -> None:
        if not self.db_ok:
            await self._async_connect()
            return
        try:
            await self._async_sync_from_db()
        except Exception as err:  # noqa: BLE001
            self._set_offline(err)

    async def _async_sync_from_db(self) -> None:
        """DB ist führend; nur wenn dort ein Plan fehlt, wird die lokale Kopie hochgeladen."""
        for plan_id in {DEFAULT_PLAN_ID, *self.plans}:
            rev = await self.hass.async_add_executor_job(self.db.get_revision_no, plan_id)
            local = self.plans.get(plan_id)
            if rev is None:
                if local and local.get("plan"):
                    _LOGGER.warning("Home Twin: Plan '%s' fehlt in der Datenbank – lade lokale Kopie hoch", plan_id)
                    await self.async_save(plan_id, local["plan"], base_revision=0, note="Aus lokaler Kopie wiederhergestellt")
                continue
            if local and local.get("revision") == rev:
                continue
            row = await self.hass.async_add_executor_job(self.db.get_plan, plan_id)
            if row:
                self.plans[plan_id] = row
                await self._async_save_cache()
                self._notify(plan_id)

    async def _async_after_start(self, _hass: HomeAssistant | None = None) -> None:
        if (self.plans.get(DEFAULT_PLAN_ID) or {}).get("plan"):
            return
        try:
            res = await self.async_import_lovelace(DEFAULT_PLAN_ID, self.import_dashboard, auto=True)
            _LOGGER.info("Home Twin: Plan aus Dashboard '%s' übernommen (Revision %s)", self.import_dashboard, res.get("revision"))
        except Exception as err:  # noqa: BLE001
            _LOGGER.info("Home Twin: kein Plan zum Übernehmen gefunden (%s)", err)

    # ------------------------------------------------------------ Lesen
    def snapshot(self, plan_id: str) -> dict:
        p = self.plans.get(plan_id) or {}
        return {
            "plan_id": plan_id,
            "revision": p.get("revision", 0),
            "plan": p.get("plan"),
            "updated_at": p.get("updated_at"),
            "updated_by": p.get("updated_by"),
            "source": "db" if self.db_ok else "cache",
            "writable": self.db_ok,
        }

    def status(self) -> dict:
        return {
            "db_connected": self.db_ok,
            "db_error": self.db_error,
            "schema": self.schema,
            "plans": {k: v.get("revision", 0) for k, v in self.plans.items()},
            "import_dashboard": self.import_dashboard,
        }

    async def async_list_revisions(self, plan_id: str, limit: int) -> list[dict]:
        self._require_db()
        return await self.hass.async_add_executor_job(self.db.list_revisions, plan_id, limit)

    async def async_get_revision(self, plan_id: str, revision: int) -> dict | None:
        self._require_db()
        return await self.hass.async_add_executor_job(self.db.get_revision, plan_id, revision)

    # ------------------------------------------------------------ Schreiben
    def _require_db(self) -> None:
        if not self.db_ok:
            raise TwinUnavailable(self.db_error or "Datenbank nicht verbunden")

    async def async_save(
        self,
        plan_id: str,
        plan: dict,
        base_revision: int | None,
        force: bool = False,
        user_id: str | None = None,
        user_name: str | None = None,
        note: str | None = None,
    ) -> dict:
        validate_plan(plan)
        async with self._lock:
            self._require_db()
            try:
                res = await self.hass.async_add_executor_job(
                    lambda: self.db.save_plan(plan_id, plan, base_revision, force, user_id, user_name, note)
                )
            except ConflictError:
                raise
            except Exception as err:  # noqa: BLE001 - DB-Fehler -> offline, Client bekommt db_unavailable
                self._set_offline(err)
                raise TwinUnavailable(self.db_error) from err
            self.plans[plan_id] = {**res, "plan": plan}
            await self._async_save_cache()
        self._notify(plan_id)
        return res

    async def async_import_lovelace(
        self, plan_id: str, dashboard: str, auto: bool = False, user_id: str | None = None, user_name: str | None = None
    ) -> dict:
        cfg = await self._async_load_lovelace(dashboard)
        plan = find_master_plan(cfg)
        if plan is None:
            raise HomeAssistantError(f"Keine Haus-Grundriss-Karte mit plan: im Dashboard '{dashboard}'")
        note = f"Import aus Lovelace-Dashboard '{dashboard}'"
        if not self.db_ok and auto:
            # Ohne DB nur lokal merken; beim nächsten Verbinden wird die Kopie hochgeladen.
            self.plans[plan_id] = {"revision": 0, "plan": plan, "updated_at": None, "updated_by": note}
            await self._async_save_cache()
            self._notify(plan_id)
            return {"revision": 0}
        return await self.async_save(
            plan_id, plan, base_revision=0 if auto else None, force=not auto,
            user_id=user_id, user_name=user_name or "Lovelace-Import", note=note,
        )

    async def _async_load_lovelace(self, url_path: str) -> Any:
        ld = self.hass.data.get("lovelace")
        dashboards = getattr(ld, "dashboards", None)
        if dashboards is None and isinstance(ld, dict):
            dashboards = ld.get("dashboards")
        dash = (dashboards or {}).get(url_path)
        if dash is not None:
            return await dash.async_load(False)
        # Rückfall: Storage-Datei direkt lesen (nur lesend)
        path = self.hass.config.path(".storage", "lovelace." + url_path.replace("-", "_"))
        data = await self.hass.async_add_executor_job(load_json, path)
        return ((data or {}).get("data") or {}).get("config")

    async def _async_save_cache(self) -> None:
        await self.store.async_save({"plans": self.plans})

    # ------------------------------------------------------------ Abonnenten
    @callback
    def async_subscribe(self, plan_id: str, cb: Callable[[dict], None]) -> Callable[[], None]:
        subs = self._listeners.setdefault(plan_id, set())
        subs.add(cb)

        @callback
        def unsub() -> None:
            subs.discard(cb)

        return unsub

    @callback
    def _notify(self, plan_id: str) -> None:
        snap = self.snapshot(plan_id)
        for cb in list(self._listeners.get(plan_id, ())):
            try:
                cb(snap)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Home Twin: Fehler beim Benachrichtigen")

    @callback
    def _notify_all(self) -> None:
        for plan_id in list(self._listeners):
            self._notify(plan_id)
