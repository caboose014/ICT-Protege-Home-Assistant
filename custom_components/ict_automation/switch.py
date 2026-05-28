import logging
from homeassistant.components.switch import SwitchEntity
<<<<<<< Updated upstream
from homeassistant.helpers.entity import DeviceInfo
from .const import DOMAIN, CONF_OUTPUTS
=======
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from .const import DOMAIN, CONF_OUTPUTS, CONF_INPUTS
>>>>>>> Stashed changes

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    client = hass.data[DOMAIN][entry.entry_id]
<<<<<<< Updated upstream
    
    # Setup Standard Outputs (Relays) ONLY
    outputs_data = entry.options.get(CONF_OUTPUTS, {})
    entities = []
    
    for k, v in outputs_data.items():
        # Handle Config Format safely
        name = v.get("name", str(v)) if isinstance(v, dict) else str(v)
        entities.append(ICTOutputSwitch(client, int(k), name))

=======
    output_data = entry.options.get(CONF_OUTPUTS, {})
    input_data = entry.options.get(CONF_INPUTS, {})
    entities = [ICTOutput(client, int(k), v) for k, v in output_data.items()]
    entities.extend(ICTInputBypassSwitch(client, int(k), v) for k, v in input_data.items())
>>>>>>> Stashed changes
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
<<<<<<< Updated upstream
        # Command 1 = On (for Relays)
        await self._client.send_command_with_pin(0x02, 1, self._dev_id, None)

    async def async_turn_off(self, **kwargs):
        # Command 2 = Off (for Relays)
        await self._client.send_command_with_pin(0x02, 2, self._dev_id, None)
=======
        await self._client.turn_output_on(self._dev_id)

    async def async_turn_off(self, **kwargs):
        await self._client.turn_output_off(self._dev_id)


class ICTInputBypassSwitch(SwitchEntity):
    def __init__(self, client, dev_id, name):
        self._client = client
        self._dev_id = dev_id
        self._attr_name = f"{name} Bypass"
        self._attr_unique_id = f"ict_input_bypass_switch_{dev_id}"
        self._attr_entity_category = EntityCategory.CONFIG
        self._attr_icon = "mdi:shield-off"
        self._is_on = False
        self._attr_extra_state_attributes = {}

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"input_{self._dev_id}")},
            name=self._attr_name.replace(" Bypass", ""),
            manufacturer="Integrated Control Technology",
            model="Protege Input",
            via_device=(DOMAIN, "ict_controller"),
        )

    async def async_added_to_hass(self):
        self._client.register_callback(self._handle_update)

    @callback
    def _handle_update(self, update):
        if update["type"] == "input" and update["id"] == self._dev_id:
            self._is_on = update.get("bypassed", False)
            self._attr_extra_state_attributes = {
                "bypass_latched": update.get("bypass_latched", False),
                "input_status": update.get("status"),
            }
            self.async_write_ha_state()

    @property
    def is_on(self):
        return self._is_on

    async def async_turn_on(self, **kwargs):
        await self._client.bypass_input(self._dev_id)

    async def async_turn_off(self, **kwargs):
        await self._client.unbypass_input(self._dev_id)
>>>>>>> Stashed changes
