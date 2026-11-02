"""Button to re-scan the panel."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ProtegeConfigEntry
from .const import CONF_RECORDS, DOMAIN
from .hub import ProtegeHub, records_to_data
from .protege import ProtegeError


async def async_setup_entry(
    hass: HomeAssistant, entry: ProtegeConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([ProtegeRescanButton(entry.runtime_data)])


class ProtegeRescanButton(ButtonEntity):
    """Re-discover records, e.g. after the installer adds doors or inputs."""

    _attr_has_entity_name = True
    _attr_translation_key = "rescan"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hub: ProtegeHub) -> None:
        self.hub = hub
        self._attr_unique_id = f"{hub.serial}_rescan"
        self._attr_device_info = hub.controller_device

    @property
    def available(self) -> bool:
        return self.hub.available

    async def async_press(self) -> None:
        try:
            records = await self.hub.client.discover()
        except ProtegeError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        data = records_to_data(records)
        entry = self.hub.entry
        if data != entry.data.get(CONF_RECORDS):
            # The update listener reloads the entry with the new records.
            self.hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_RECORDS: data})
