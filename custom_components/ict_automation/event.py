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
    ("trouble", ("trouble", "fail", "tamper", "battery", "offline")),
]
# The panel's first event field is a category; map it when no keyword above applies.
_CATEGORIES = {
    "door": "door",
    "input": "input",
    "output": "output",
    "user": "user",
    "trouble": "trouble",
}


def classify(event: PanelEvent) -> str:
    category = (event.category or "").strip().lower()
    description = (event.description or event.text or "").lower()
    if category in ("output", "input") and "alarm" not in description and "tamper" not in description:
        # "Output ... By Door Front Door" is about the output, not the door.
        return category
    for event_type, words in _KEYWORDS:
        if any(word in description for word in words):
            return event_type
    if category in _CATEGORIES:
        return _CATEGORIES[category]
    if any(word in description for word in ("door", "forced", "left open")):
        return "door"
    return "other"


async def async_setup_entry(
    hass: HomeAssistant, entry: ProtegeConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    hub = entry.runtime_data
    if hub.has_events:
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
        self._trigger_event(
            classify(event),
            {"text": event.text, "category": event.category, "user": event.user, "code": event.code},
        )
        self.async_write_ha_state()
