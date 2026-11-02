"""Doors as locks."""

from __future__ import annotations

from typing import Any

from homeassistant.components.lock import LockEntity, LockEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ProtegeConfigEntry
from .entity import ProtegeEntity
from .protege import DoorLockState, DoorStatus, RecordType


async def async_setup_entry(
    hass: HomeAssistant, entry: ProtegeConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    hub = entry.runtime_data
    async_add_entities(ProtegeDoorLock(hub, r) for r in hub.records_of(RecordType.DOOR))


class ProtegeDoorLock(ProtegeEntity, LockEntity):
    """Lock = lock, unlock = unlock latched, open = momentary unlock."""

    _attr_name = None
    _attr_supported_features = LockEntityFeature.OPEN

    def __init__(self, hub, record) -> None:
        super().__init__(hub, record, "lock", hub.record_device(record))

    @property
    def is_locked(self) -> bool | None:
        status: DoorStatus | None = self.status
        return None if status is None else status.locked

    @property
    def is_open(self) -> bool | None:
        status: DoorStatus | None = self.status
        if status is None:
            return None
        return not status.locked and status.open

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        status: DoorStatus | None = self.status
        if status is None:
            return None
        state = status.lock_state
        return {"lock_reason": state.name.lower() if isinstance(state, DoorLockState) else state}

    async def async_lock(self, **kwargs: Any) -> None:
        await self._run(self.hub.client.door_lock(self.record.index))

    async def async_unlock(self, **kwargs: Any) -> None:
        await self._run(self.hub.client.door_unlock_latched(self.record.index))

    async def async_open(self, **kwargs: Any) -> None:
        await self._run(self.hub.client.door_unlock(self.record.index))
