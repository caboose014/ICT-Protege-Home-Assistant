"""Areas as alarm control panels."""

from __future__ import annotations

import asyncio
import contextlib
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
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ProtegeConfigEntry
from .const import ARM_MODES, DOMAIN
from .entity import ProtegeEntity
from .protege import AreaState, AreaStatus, ProtegeError, RecordType
from .protege import protocol as p

SERVICE_FORCE_ARM = "force_arm"
ARM_CHECK_DELAY = 1.0

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

    @property
    def _modes(self) -> set[str]:
        return set(ARM_MODES) - self.hub.disabled_arm_modes[self.record.index]

    @property
    def supported_features(self) -> AlarmControlPanelEntityFeature:
        """Arm modes can be turned off per area with the area's configuration switches."""
        features = AlarmControlPanelEntityFeature(0)
        for mode in self._modes:
            features |= _FEATURES[mode]
        return features

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
        if mode is not None or sub_command == p.AREA_ARM_FORCE:
            await self._check_armed()

    async def _check_armed(self) -> None:
        """The panel accepts an arm request even when open zones stop it arming; say which."""
        status: AreaStatus | None = self.status
        if status is not None and not status.armed:
            # Give the panel a moment to start its exit delay before calling it a failure.
            await asyncio.sleep(ARM_CHECK_DELAY)
            with contextlib.suppress(ProtegeError):
                await self.hub.client.request_status(RecordType.AREA, self.record.index)
            status = self.status
        if status is None or status.armed:
            return
        zones = self.hub.open_zones(self.record.index)
        if status.state == AreaState.INPUTS_OPEN or zones:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="arm_failed_open_zones" if zones else "arm_failed",
                translation_placeholders={"zones": ", ".join(zones or [])},
            )

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
