import logging
from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.entity import DeviceInfo
from .const import (
    DOMAIN, CONF_OUTPUTS, CONF_INPUTS,
    CMD_INPUT_BYPASS, CMD_INPUT_UNBYPASS
)

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    client = hass.data[DOMAIN][entry.entry_id]
    entities = []

    # 1. Setup Standard Outputs (Relays)
    outputs_data = entry.options.get(CONF_OUTPUTS, {})
    for k, v in outputs_data.items():
        # Handle legacy vs new config formats if needed
        name = v.get("name", str(v)) if isinstance(v, dict) else str(v)
        entities.append(ICTOutputSwitch(client, int(k), name))

    # 2. Setup Input Bypass Switches
    inputs_data = entry.options.get(CONF_INPUTS, {})
    for k, v in inputs_data.items():
        dev_id = int(k)
        if isinstance(v, dict):
            name = v.get("name", f"Input {dev_id}")
        else:
            name = str(v)
            
        entities.append(ICTBypassSwitch(client, dev_id, name))

    async_add_entities(entities)

# --- CLASS 1: Standard Output (Relay) ---
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
            # via_device removed to fix error
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
        # Command 1 = On (for Outputs)
        await self._client.send_command_with_pin(0x02, 1, self._dev_id, None)

    async def async_turn_off(self, **kwargs):
        # Command 2 = Off (for Outputs)
        await self._client.send_command_with_pin(0x02, 2, self._dev_id, None)


# --- CLASS 2: Input Bypass Switch ---
class ICTBypassSwitch(SwitchEntity):
    def __init__(self, client, dev_id, name):
        self._client = client
        self._dev_id = dev_id
        self._attr_name = f"{name} Bypass"
        self._attr_unique_id = f"ict_input_bypass_{dev_id}"
        self._is_bypassed = False
        self._attr_icon = "mdi:shield-remove-outline" # Distinct icon

    @property
    def device_info(self) -> DeviceInfo:
        # Link this switch to the INPUT device
        return DeviceInfo(
            identifiers={(DOMAIN, f"input_{self._dev_id}")},
            name=self._attr_name,
            manufacturer="Integrated Control Technology",
            model="Protege Input",
            # via_device removed to fix error
        )

    async def async_added_to_hass(self):
        self._client.register_callback(self._handle_update)

    def _handle_update(self, update):
        # Listen for input updates to check 'bypassed' status
        if update["type"] == "input" and update["id"] == self._dev_id:
            # Use .get() to be safe
            self._is_bypassed = update.get("bypassed", False)
            self.async_write_ha_state()

    @property
    def is_on(self):
        return self._is_bypassed

    async def async_turn_on(self, **kwargs):
        """Enable Bypass."""
        # 0x04 is likely the Input Object Type (check your library constants)
        # Using Command 1 (Bypass)
        await self._client.send_command_with_pin(0x04, CMD_INPUT_BYPASS, self._dev_id, None)

    async def async_turn_off(self, **kwargs):
        """Disable Bypass."""
        # Using Command 2 (Un-Bypass)
        await self._client.send_command_with_pin(0x04, CMD_INPUT_UNBYPASS, self._dev_id, None)