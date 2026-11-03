"""Show panel events in the Home Assistant activity log."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.logbook import LOGBOOK_ENTRY_MESSAGE, LOGBOOK_ENTRY_NAME
from homeassistant.core import Event, HomeAssistant, callback

from .const import DOMAIN, EVENT_PANEL


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, str]]], None],
) -> None:
    @callback
    def describe(event: Event) -> dict[str, str]:
        text = event.data.get("text") or f"Event code {event.data.get('code')}"
        return {LOGBOOK_ENTRY_NAME: "ICT Protege", LOGBOOK_ENTRY_MESSAGE: text}

    async_describe_event(DOMAIN, EVENT_PANEL, describe)
