import logging
from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.entity import DeviceInfo
from .const import DOMAIN, CONF_DOORS

async def async_setup_entry(hass, entry, async_add_entities):
    client = hass.data[DOMAIN][entry.entry_id]
    data = entry.options.get(CONF_DOORS, {})
    entities = [ICTDoor(client, int(k), v) for k, v in data.items()]
    async_add_entities(entities)

class ICTDoor(LockEntity):
    def __init__(self, client, door_id, name):
        self._client = client
        self._door_id = door_id
        self._attr_name = name
        self._attr_unique_id = f"ict_door_{door_id}"
        self._is_locked = True
        self._is_open = False

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

    @callback
    def _handle_update(self, update):
        if update["type"] == "door" and update["id"] == self._door_id:
            self._is_locked = update["locked"]
            self._is_open = update["open"]
            self.async_write_ha_state()

    @property
    def is_locked(self): return self._is_locked
    
    @property
    def is_open(self): return self._is_open

    async def async_lock(self, **kwargs):
        await self._client.send_command(0x01, 0x00, self._door_id)

    async def async_unlock(self, **kwargs):
        await self._client.send_command(0x01, 0x02, self._door_id)