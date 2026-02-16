import logging
from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.entity import DeviceInfo
from .const import DOMAIN, CONF_DOORS, CMD_DOOR_UNLOCK_MOMENTARY

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    client = hass.data[DOMAIN][entry.entry_id]
    data = entry.options.get(CONF_DOORS, {})
    
    entities = []
    for k, v in data.items():
        # Handle config format safely
        name = v.get("name", str(v)) if isinstance(v, dict) else str(v)
        entities.append(ICTDoorButton(client, int(k), name))
        
    async_add_entities(entities)

class ICTDoorButton(ButtonEntity):
    def __init__(self, client, door_id, name):
        self._client = client
        self._door_id = door_id
        self._attr_name = f"{name} Momentary"
        self._attr_unique_id = f"ict_door_btn_{door_id}"
        self._attr_icon = "mdi:doorbell" # Icon for the button

    @property
    def device_info(self) -> DeviceInfo:
        # Links this button to the same Device as the Lock
        return DeviceInfo(
            identifiers={(DOMAIN, f"door_{self._door_id}")},
            name=self._attr_name,
            manufacturer="Integrated Control Technology",
            model="Protege Door",
            via_device=(DOMAIN, "ict_controller"),
        )

    async def async_press(self) -> None:
        """Handle the button press."""
        # SEND COMMAND 3: Momentary Unlock
        await self._client.send_command_with_pin(0x01, CMD_DOOR_UNLOCK_MOMENTARY, self._door_id, None)