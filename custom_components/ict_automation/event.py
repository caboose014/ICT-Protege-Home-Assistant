"""Panel system events (needs the optional events service)."""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ProtegeConfigEntry
from .const import EVENT_TYPES
from .hub import ProtegeHub
from .protege import PanelEvent

_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("access_denied", ("denied",)),
    ("access_granted", ("granted",)),
    ("disarmed", ("disarm",)),
    ("armed", ("armed", "arming")),
    ("alarm", ("alarm",)),
    ("door", ("door", "forced", "left open")),
    ("trouble", ("trouble", "fail", "tamper", "battery", "offline")),
]


def classify(text: str | None) -> str:
    lowered = (text or "").lower()
    for event_type, words in _KEYWORDS:
        if any(word in lowered for word in words):
            return event_type
    return "other"


async def async_setup_entry(
    hass: HomeAssistant, entry: ProtegeConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    hub = entry.runtime_data
    if hub.events is not None:
        async_add_entities([ProtegePanelEvent(hub)])


class ProtegePanelEvent(EventEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "panel_event"
    _attr_event_types = EVENT_TYPES

    def __init__(self, hub: ProtegeHub) -> None:
        self.hub = hub
        self._attr_unique_id = f"{hub.serial}_panel_event"
        self._attr_device_info = hub.controller_device

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.hub.async_add_event_listener(self._handle))

    @callback
    def _handle(self, event: PanelEvent) -> None:
        self._trigger_event(classify(event.text), {"text": event.text, "code": event.code})
        self.async_write_ha_state()
