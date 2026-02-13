import logging
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.entity import DeviceInfo
from .const import DOMAIN, CONF_OUTPUTS

async def async_setup_entry(hass, entry, async_add_entities):
    client = hass.data[DOMAIN][entry.entry_id]
    data = entry.options.get(CONF_OUTPUTS, {})
    entities = [ICTOutput(client, int(k), v) for k, v in data.items()]
    async_add_entities(entities)

class ICTOutput(SwitchEntity):
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
            via_device=(DOMAIN, "ict_controller"),
        )

    async def async_added_to_hass(self):
        self._client.register_callback(self._handle_update)

    @callback
    def _handle_update(self, update):
        if update["type"] == "output" and update["id"] == self._dev_id:
            self._is_on = update["on"]
            self.async_write_ha_state()

    @property
    def is_on(self): return self._is_on

    async def async_turn_on(self, **kwargs):
        await self._client.send_command(0x03, 0x01, self._dev_id)

    async def async_turn_off(self, **kwargs):
        await self._client.send_command(0x03, 0x00, self._dev_id)