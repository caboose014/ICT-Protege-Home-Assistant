"""Diagnostics for ICT Protege."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from . import ProtegeConfigEntry
from .const import CONF_EVENTS_PIN, CONF_PIN

TO_REDACT = {CONF_PIN, CONF_EVENTS_PIN, CONF_HOST}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ProtegeConfigEntry
) -> dict[str, Any]:
    hub = entry.runtime_data
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "panel": asdict(hub.panel),
        "available": hub.available,
        "events_connected": bool(hub.events and hub.events.connected),
        "status": {
            f"{rtype.value}_{index}": repr(status) for (rtype, index), status in hub.client.status.items()
        },
    }
