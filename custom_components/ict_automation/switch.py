import logging
from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.entity import DeviceInfo
from .const import DOMAIN, CONF_OUTPUTS

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    client = hass.data[DOMAIN][entry.entry_id]
    
    # Setup Standard Outputs (Relays) ONLY
    outputs_data = entry.options.get(CONF_OUTPUTS, {})
    entities = []
    
    for k, v in outputs_data.items():
        # Handle Config Format safely
        name = v.get("name", str(v)) if isinstance(v, dict) else str(v)
        entities.append(ICTOutputSwitch(client, int(k), name))

    async_add_entities(entities)

class ICTOutputSwitch(SwitchEntity):
    def __init__(self, client, dev_id, name):
        self._client = client
        self._dev_id = dev_id
        self._attr_name = name
        self._attr_unique_id = f"ict_output_{dev_id}"
        self._is_on = False

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"output_{self._dev_id}")},
            name=self._attr_name,
            manufacturer="Integrated Control Technology",
            model="Protege Output",
            # Note: via_device is removed to prevent registry errors
        )

    async def async_added_to_hass(self):
        self._client.register_callback(self._handle_update)

    def _handle_update(self, update):
        if update["type"] == "output" and update["id"] == self._dev_id:
            self._is_on = update["on"]
            self.async_write_ha_state()

    @property
    def is_on(self):
        return self._is_on

    async def async_turn_on(self, **kwargs):
        # Command 1 = On (for Relays)
        await self._client.send_command_with_pin(0x02, 1, self._dev_id, None)

    async def async_turn_off(self, **kwargs):
        # Command 2 = Off (for Relays)
        await self._client.send_command_with_pin(0x02, 2, self._dev_id, None)
