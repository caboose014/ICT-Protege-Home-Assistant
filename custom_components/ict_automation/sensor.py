"""Detailed door and input states."""

from __future__ import annotations

from enum import IntEnum

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ProtegeConfigEntry
from .entity import ProtegeEntity
from .protege import DoorState, InputState, RecordType


async def async_setup_entry(
    hass: HomeAssistant, entry: ProtegeConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    hub = entry.runtime_data
    entities: list[SensorEntity] = [ProtegeDoorState(hub, r) for r in hub.records_of(RecordType.DOOR)]
    entities.extend(ProtegeInputState(hub, r) for r in hub.records_of(RecordType.INPUT))
    async_add_entities(entities)


def _enum_value(value: IntEnum | int) -> str | None:
    return value.name.lower() if isinstance(value, IntEnum) else None


class ProtegeDoorState(ProtegeEntity, SensorEntity):
    """Closed, open, left open, forced open..."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [s.name.lower() for s in DoorState]
    _attr_translation_key = "door_state"

    def __init__(self, hub, record) -> None:
        super().__init__(hub, record, "door_state", hub.record_device(record))

    @property
    def native_value(self) -> str | None:
        return None if self.status is None else _enum_value(self.status.door_state)


class ProtegeInputState(ProtegeEntity, SensorEntity):
    """Closed, open, short or tamper. Disabled by default."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [s.name.lower() for s in InputState]
    _attr_translation_key = "input_state"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, hub, record) -> None:
        super().__init__(hub, record, "input_state", hub.module_device(record.module))
        self._attr_translation_placeholders = {"name": record.default_name}

    @property
    def native_value(self) -> str | None:
        return None if self.status is None else _enum_value(self.status.state)
