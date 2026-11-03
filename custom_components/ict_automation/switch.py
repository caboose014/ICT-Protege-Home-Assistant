"""Outputs and input bypass."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ProtegeConfigEntry
from .const import DOMAIN
from .entity import ProtegeEntity
from .protege import RecordType

SERVICE_TURN_ON_FOR = "turn_on_for"
SERVICE_BYPASS = "bypass"
ATTR_DURATION = "duration"
ATTR_PERMANENT = "permanent"


async def async_setup_entry(
    hass: HomeAssistant, entry: ProtegeConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    hub = entry.runtime_data
    entities: list[SwitchEntity] = [ProtegeOutput(hub, r) for r in hub.records_of(RecordType.OUTPUT)]
    entities.extend(ProtegeBypass(hub, r) for r in hub.records_of(RecordType.INPUT))
    async_add_entities(entities)

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_TURN_ON_FOR,
        {vol.Required(ATTR_DURATION): vol.All(cv.time_period, cv.positive_timedelta)},
        "async_turn_on_for",
    )
    platform.async_register_entity_service(
        SERVICE_BYPASS, {vol.Optional(ATTR_PERMANENT, default=False): cv.boolean}, "async_bypass"
    )


class ProtegeOutput(ProtegeEntity, SwitchEntity):
    def __init__(self, hub, record) -> None:
        super().__init__(hub, record, "output", hub.record_device(record))
        self._attr_name = None  # the output's own device carries its name

    @property
    def is_on(self) -> bool | None:
        return None if self.status is None else self.status.on

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._run(self.hub.client.output_on(self.record.index))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._run(self.hub.client.output_off(self.record.index))

    async def async_turn_on_for(self, duration) -> None:
        seconds = max(1, min(65535, int(duration.total_seconds())))
        await self._run(self.hub.client.output_on(self.record.index, seconds=seconds))

    async def async_bypass(self, permanent: bool = False) -> None:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="bypass_inputs_only")


class ProtegeBypass(ProtegeEntity, SwitchEntity):
    """On = bypassed (until the area next disarms). Use the bypass action for permanent."""

    _attr_translation_key = "bypass"

    def __init__(self, hub, record) -> None:
        super().__init__(hub, record, "bypass", hub.record_device(record))

    @property
    def is_on(self) -> bool | None:
        return None if self.status is None else self.status.bypassed

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return None if self.status is None else {"latched": self.status.bypass_latched}

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.async_bypass(False)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._run(self.hub.client.set_bypass(RecordType.INPUT, self.record.index, False))

    async def async_bypass(self, permanent: bool = False) -> None:
        await self._run(
            self.hub.client.set_bypass(RecordType.INPUT, self.record.index, True, permanent=permanent)
        )

    async def async_turn_on_for(self, duration) -> None:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="timed_outputs_only")
