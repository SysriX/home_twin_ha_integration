"""Integrationseintrag für Home Twin.

Home Twin wird weiter per YAML konfiguriert (db_url). Der Eintrag wird beim Start
automatisch angelegt und dient nur dazu, für hinzugefügte BLE-Tags Geräte und
Entitäten in HA anlegen zu können.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN


class HomeTwinConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="Home Twin", data={})

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        # Einrichtung läuft über configuration.yaml (home_twin: db_url: …)
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        return self.async_abort(reason="yaml_only")
