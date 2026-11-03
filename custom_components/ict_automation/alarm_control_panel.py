"""Areas as alarm control panels."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
    CodeFormat,
)
from homeassistant.const import ATTR_CODE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ProtegeConfigEntry
from .const import ARM_MODES, CONF_ARM_MODES, DOMAIN
from .entity import ProtegeEntity
from .protege import AreaState, AreaStatus, RecordType
from .protege import protocol as p

SERVICE_FORCE_ARM = "force_arm"

_FEATURES = {
    "away": AlarmControlPanelEntityFeature.ARM_AWAY,
    "home": AlarmControlPanelEntityFeature.ARM_HOME,
    "night": AlarmControlPanelEntityFeature.ARM_NIGHT,
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ProtegeConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    hub = entry.runtime_data
    async_add_entities(ProtegeArea(hub, r) for r in hub.records_of(RecordType.AREA))
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_FORCE_ARM, {vol.Required(ATTR_CODE): cv.string}, "async_force_arm"
    )


class ProtegeArea(ProtegeEntity, AlarmControlPanelEntity):
    """A Protege area. Arming always uses the person's own code."""

    _attr_name = None
    _attr_code_format = CodeFormat.NUMBER
    _attr_code_arm_required = True

    def __init__(self, hub, record) -> None:
        super().__init__(hub, record, "area", hub.record_device(record))
        modes = hub.entry.options.get(CONF_ARM_MODES, ARM_MODES)
        features = AlarmControlPanelEntityFeature(0)
        for mode in modes:
            features |= _FEATURES.get(mode, AlarmControlPanelEntityFeature(0))
        self._attr_supported_features = features
        self._modes = set(modes)

    @property
    def alarm_state(self) -> AlarmControlPanelState | None:
        status: AreaStatus | None = self.status
        if status is None:
            return None
        if status.alarm:
            return AlarmControlPanelState.TRIGGERED
        if status.state == AreaState.EXIT_DELAY:
            return AlarmControlPanelState.ARMING
        if status.state in (AreaState.ENTRY_DELAY, AreaState.CODE_DELAY):
            return AlarmControlPanelState.PENDING
        if status.state == AreaState.DISARM_DELAY:
            return AlarmControlPanelState.DISARMING
        if status.armed:
            if status.partial_armed:
                return AlarmControlPanelState.ARMED_HOME
            if status.instant_armed:
                return AlarmControlPanelState.ARMED_NIGHT
            return AlarmControlPanelState.ARMED_AWAY
        return AlarmControlPanelState.DISARMED

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        status: AreaStatus | None = self.status
        if status is None:
            return None
        state = status.state
        return {
            "panel_state": state.name.lower() if isinstance(state, AreaState) else state,
            "force_armed": status.force_armed,
            "alarm_memory": status.alarm_memory,
            "siren": status.siren,
        }

    async def _area(self, sub_command: int, code: str | None, mode: str | None = None) -> None:
        if mode is not None and mode not in self._modes:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="arm_mode_disabled")
        if not code:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="code_required")
        await self._run(self.hub.client.area_command(self.record.index, sub_command, code))

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        await self._area(p.AREA_DISARM_ALL, code)

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        await self._area(p.AREA_ARM_NORMAL, code, "away")

    async def async_alarm_arm_home(self, code: str | None = None) -> None:
        await self._area(p.AREA_ARM_STAY, code, "home")

    async def async_alarm_arm_night(self, code: str | None = None) -> None:
        await self._area(p.AREA_ARM_INSTANT, code, "night")

    async def async_force_arm(self, code: str) -> None:
        await self._area(p.AREA_ARM_FORCE, code)
