"""Repairs: duress, panic and medical alarms, and moving onto the control service."""

from __future__ import annotations

from typing import Any

from homeassistant import data_entry_flow
from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.core import HomeAssistant

from .config_flow import upgrade_errors, upgrade_schema
from .upgrade import async_upgrade, control_port


class UpgradeRepairFlow(RepairsFlow):
    """The control service answered on a web-only install: switch over with the integration PIN."""

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> data_entry_flow.FlowResult:
        return await self.async_step_upgrade()

    async def async_step_upgrade(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        if entry is None:
            return self.async_abort(reason="entry_missing")
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await async_upgrade(self.hass, entry, int(user_input["port"]), str(user_input["pin"]))
            except Exception as err:  # noqa: BLE001
                errors = upgrade_errors(err)
            else:
                return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="upgrade",
            data_schema=upgrade_schema(user_input or {}, control_port(entry)),
            errors=errors,
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str | int | float | None] | None
) -> RepairsFlow:
    if issue_id.startswith("acs_available_") and data and data.get("entry_id"):
        return UpgradeRepairFlow(str(data["entry_id"]))
    # Marking an emergency handled just clears it.
    return ConfirmRepairFlow()
