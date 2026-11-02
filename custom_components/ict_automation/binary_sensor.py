"""Door contacts, inputs, trouble inputs and area alarms."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ProtegeConfigEntry
from .entity import ProtegeEntity
from .protege import InputState, RecordType


async def async_setup_entry(
    hass: HomeAssistant, entry: ProtegeConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    hub = entry.runtime_data
    entities: list[BinarySensorEntity] = []
    for record in hub.records_of(RecordType.DOOR):
        entities.append(ProtegeDoorContact(hub, record))
    for record in hub.records_of(RecordType.AREA):
        entities.append(ProtegeAreaFlag(hub, record, "alarm"))
        entities.append(ProtegeAreaFlag(hub, record, "siren"))
    for record in hub.records_of(RecordType.INPUT):
        entities.append(ProtegeInput(hub, record))
    for record in hub.records_of(RecordType.TROUBLE_INPUT):
        entities.append(ProtegeTroubleInput(hub, record))
    async_add_entities(entities)


class ProtegeDoorContact(ProtegeEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.DOOR
    _attr_translation_key = "door_contact"

    def __init__(self, hub, record) -> None:
        super().__init__(hub, record, "contact", hub.record_device(record))

    @property
    def is_on(self) -> bool | None:
        return None if self.status is None else self.status.open


class ProtegeAreaFlag(ProtegeEntity, BinarySensorEntity):
    def __init__(self, hub, record, flag: str) -> None:
        super().__init__(hub, record, flag, hub.record_device(record))
        self._flag = flag
        self._attr_translation_key = flag
        if flag == "alarm":
            self._attr_device_class = BinarySensorDeviceClass.SAFETY
        else:
            self._attr_device_class = BinarySensorDeviceClass.SOUND

    @property
    def is_on(self) -> bool | None:
        return None if self.status is None else getattr(self.status, self._flag)


class ProtegeInput(ProtegeEntity, BinarySensorEntity):
    """An input (zone). Change its type with "Show as" in the entity settings."""

    def __init__(self, hub, record) -> None:
        super().__init__(hub, record, "input", hub.module_device(record.module))
        self._attr_name = record.default_name

    @property
    def is_on(self) -> bool | None:
        return None if self.status is None else self.status.active

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        status = self.status
        if status is None:
            return None
        state = status.state
        return {
            "reference": status.reference,
            "input_state": state.name.lower() if isinstance(state, InputState) else state,
            "bypassed": status.bypassed,
        }


class ProtegeTroubleInput(ProtegeInput):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, hub, record) -> None:
        ProtegeEntity.__init__(self, hub, record, "trouble", hub.controller_device)
        self._attr_name = record.default_name
