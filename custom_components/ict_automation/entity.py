"""Base entity for ICT Protege."""

from __future__ import annotations

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .hub import ProtegeHub
from .protege import ProtegeError, Record, RecordType


class ProtegeEntity(Entity):
    """An entity backed by one panel record."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: ProtegeHub, record: Record, key: str, device: DeviceInfo) -> None:
        self.hub = hub
        self.record = record
        self._attr_unique_id = f"{hub.record_device_id(record)}_{key}"
        self._attr_device_info = device

    @property
    def status(self):
        return self.hub.client.status.get(self.record.key)

    @property
    def available(self) -> bool:
        return self.hub.available and self.status is not None

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.hub.async_add_listener(self.record.key, self.async_write_ha_state))

    async def _run(self, coro) -> None:
        """Run a panel command and surface panel refusals as HA errors."""
        try:
            await coro
        except ProtegeError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err


def module_entity_name(record: Record) -> str:
    """Name for an input/output entity that lives on a shared module device."""
    return record.default_name


__all__ = ["ProtegeEntity", "RecordType", "module_entity_name"]
