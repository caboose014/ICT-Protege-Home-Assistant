"""Buttons: momentary door unlock and panel rescan."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ProtegeConfigEntry
from .const import CONF_RECORDS, DOMAIN
from .entity import ProtegeEntity
from .hub import ProtegeHub, async_discover_records, records_to_data
from .protege import ProtegeError, RecordType
from .upgrade import async_check_control_service


async def async_setup_entry(
    hass: HomeAssistant, entry: ProtegeConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    hub = entry.runtime_data
    entities: list[ButtonEntity] = [ProtegeRescanButton(hub)]
    entities.extend(ProtegeDoorMomentaryButton(hub, r) for r in hub.records_of(RecordType.DOOR))
    async_add_entities(entities)


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
            if self.hub.web:
                records = await self.hub.client.discover()
            else:
                records = await async_discover_records(self.hass, self.hub.entry, self.hub.client)
        except ProtegeError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        if self.hub.web:
            # See if the control service is there now.
            await async_check_control_service(self.hass, self.hub.entry)
        data = records_to_data(records)
        entry = self.hub.entry
        if data != entry.data.get(CONF_RECORDS):
            # The update listener reloads the entry with the new records.
            self.hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_RECORDS: data})


class ProtegeDoorMomentaryButton(ProtegeEntity, ButtonEntity):
    """Timed unlock: the panel relocks the door after its programmed unlock time."""

    _attr_translation_key = "momentary_unlock"

    def __init__(self, hub, record) -> None:
        super().__init__(hub, record, "momentary_unlock", hub.record_device(record))

    @property
    def available(self) -> bool:
        return self.hub.available

    async def async_press(self) -> None:
        await self._run(self.hub.client.door_unlock(self.record.index))
