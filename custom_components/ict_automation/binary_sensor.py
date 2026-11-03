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
        if hub.zones_known:
            entities.append(ProtegeReadyToArm(hub, record))
    for record in hub.records_of(RecordType.INPUT):
        entities.append(ProtegeInput(hub, record))
        entities.append(ProtegeInputTamper(hub, record))
    for record in hub.records_of(RecordType.TROUBLE_INPUT):
        entities.append(ProtegeTroubleInput(hub, record))
    entities.append(ProtegeFault(hub))
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


class ProtegeReadyToArm(ProtegeEntity, BinarySensorEntity):
    """On when no open zone stops the area arming."""

    _attr_translation_key = "ready_to_arm"

    def __init__(self, hub, record) -> None:
        super().__init__(hub, record, "ready_to_arm", hub.record_device(record))

    @property
    def is_on(self) -> bool | None:
        zones = self.hub.open_zones(self.record.index)
        return None if zones is None else not zones

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"open_zones": self.hub.open_zones(self.record.index) or []}


class ProtegeInput(ProtegeEntity, BinarySensorEntity):
    """An input (zone). Change its type with "Show as" in the entity settings."""

    def __init__(self, hub, record) -> None:
        super().__init__(hub, record, "input", hub.record_device(record))
        self._attr_name = None  # the input's own device carries its name

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


class ProtegeInputTamper(ProtegeEntity, BinarySensorEntity):
    """On when the input's wiring reports a tamper or a short."""

    _attr_device_class = BinarySensorDeviceClass.TAMPER
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "tamper"

    def __init__(self, hub, record) -> None:
        super().__init__(hub, record, "tamper", hub.record_device(record))

    @property
    def is_on(self) -> bool | None:
        return None if self.status is None else self.status.tampered


class ProtegeFault(BinarySensorEntity):
    """One sensor for "something needs attention": an active trouble input or a tampered input."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_translation_key = "fault"

    def __init__(self, hub) -> None:
        self.hub = hub
        self._attr_unique_id = f"{hub.serial}_fault"
        self._attr_device_info = hub.controller_device

    def _active(self) -> list[str] | None:
        active = []
        for record_type in (RecordType.TROUBLE_INPUT, RecordType.INPUT):
            for record in self.hub.records_of(record_type):
                status = self.hub.client.status.get(record.key)
                if status is None:
                    return None
                if record_type is RecordType.TROUBLE_INPUT and status.active:
                    active.append(record.default_name)
                elif record_type is RecordType.INPUT and status.tampered:
                    active.append(f"{record.default_name} tamper")
        return active

    @property
    def available(self) -> bool:
        return self.hub.available

    @property
    def is_on(self) -> bool | None:
        active = self._active()
        return None if active is None else bool(active)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"active": self._active() or []}

    async def async_added_to_hass(self) -> None:
        for record_type in (RecordType.TROUBLE_INPUT, RecordType.INPUT):
            for record in self.hub.records_of(record_type):
                self.async_on_remove(self.hub.async_add_listener(record.key, self.async_write_ha_state))
