"""Detailed door and input states."""

from __future__ import annotations

from enum import IntEnum
from typing import Any

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
    for area in hub.records_of(RecordType.AREA):
        entities.append(ProtegeAlarmCause(hub, area))
        if hub.zones_known:
            entities.append(ProtegeOpenZones(hub, area))
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
        super().__init__(hub, record, "input_state", hub.record_device(record))

    @property
    def native_value(self) -> str | None:
        return None if self.status is None else _enum_value(self.status.state)


class ProtegeOpenZones(ProtegeEntity, SensorEntity):
    """How many zones are open that stop the area arming, with their names.

    Zones whose input type lets them stay open when arming are not counted.
    """

    _attr_translation_key = "open_zones"
    _attr_native_unit_of_measurement = "zones"

    def __init__(self, hub, record) -> None:
        super().__init__(hub, record, "open_zones", hub.record_device(record))

    @property
    def native_value(self) -> int | None:
        zones = self.hub.open_zones(self.record.index)
        return None if zones is None else len(zones)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"zones": self.hub.open_zones(self.record.index) or []}


class ProtegeAlarmCause(ProtegeEntity, SensorEntity):
    """The zone that set the area's alarm off, kept until the area is disarmed."""

    _attr_translation_key = "alarm_cause"

    def __init__(self, hub, record) -> None:
        super().__init__(hub, record, "alarm_cause", hub.record_device(record))

    @property
    def native_value(self) -> str | None:
        return self.hub.alarm_cause.get(self.record.index)
