import logging
from homeassistant.components.lock import LockEntity
from homeassistant.helpers.entity import DeviceInfo
from .const import DOMAIN, CONF_DOORS, CMD_DOOR_LOCK, CMD_DOOR_UNLOCK_LATCH

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    client = hass.data[DOMAIN][entry.entry_id]
    # Retrieve door data safely
    data = entry.options.get(CONF_DOORS, {})
    
    entities = []
    for k, v in data.items():
        # Handle legacy config (string) vs new config (dict) if you add door types later
        name = v.get("name", str(v)) if isinstance(v, dict) else str(v)
        entities.append(ICTDoorLock(client, int(k), name))
        
    async_add_entities(entities)

class ICTDoorLock(LockEntity):
    def __init__(self, client, door_id, name):
        self._client = client
        self._door_id = door_id
        self._attr_name = name
        self._attr_unique_id = f"ict_door_{door_id}"
        self._is_locked = True 

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"door_{self._door_id}")},
            name=self._attr_name,
            manufacturer="Integrated Control Technology",
            model="Protege Door",
            via_device=(DOMAIN, "ict_controller"),
        )

    async def async_added_to_hass(self):
        self._client.register_callback(self._handle_update)

    def _handle_update(self, update):
        if update["type"] == "door" and update["id"] == self._door_id:
            # Update Lock State (Locked vs Unlocked)
            self._is_locked = update["locked"]
            self.async_write_ha_state()

    @property
    def is_locked(self): return self._is_locked

    async def async_lock(self, **kwargs) -> None:
        """Lock the door."""
        code = kwargs.get("code", None)
        # SEND COMMAND 1: Lock
        await self._client.send_command_with_pin(0x01, CMD_DOOR_LOCK, self._door_id, code)

    async def async_unlock(self, **kwargs) -> None:
        """Unlock the door (Latch/Hold Open)."""
        code = kwargs.get("code", None)
        # SEND COMMAND 2: Latch Unlock
        await self._client.send_command_with_pin(0x01, CMD_DOOR_UNLOCK_LATCH, self._door_id, code)