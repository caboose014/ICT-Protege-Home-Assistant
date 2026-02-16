import logging
from homeassistant.components.select import SelectEntity
from homeassistant.helpers.entity import DeviceInfo
from .const import (
    DOMAIN, CONF_INPUTS,
    CMD_INPUT_BYPASS_ONCE, 
    CMD_INPUT_UNBYPASS, 
    CMD_INPUT_BYPASS_PERMANENT
)

_LOGGER = logging.getLogger(__name__)

# Define the 3 States
OPTION_ACTIVE = "Active"
OPTION_BYPASS_ONCE = "Bypassed (Until Disarm)"
OPTION_BYPASS_PERM = "Bypassed (Permanent)"

async def async_setup_entry(hass, entry, async_add_entities):
    client = hass.data[DOMAIN][entry.entry_id]
    
    inputs_data = entry.options.get(CONF_INPUTS, {})
    entities = []
    
    for k, v in inputs_data.items():
        dev_id = int(k)
        if isinstance(v, dict):
            name = v.get("name", f"Input {dev_id}")
        else:
            name = str(v)
            
        entities.append(ICTBypassSelect(client, dev_id, name))
        
    async_add_entities(entities)

class ICTBypassSelect(SelectEntity):
    def __init__(self, client, dev_id, name):
        self._client = client
        self._dev_id = dev_id
        self._attr_name = f"{name} Status"
        self._attr_unique_id = f"ict_input_bypass_{dev_id}"
        # The 3 options you requested
        self._attr_options = [OPTION_ACTIVE, OPTION_BYPASS_ONCE, OPTION_BYPASS_PERM]
        self._bypass_status = 0 # 0=Active, 1=Once, 2=Perm
        self._attr_icon = "mdi:shield-check"

    @property
    def current_option(self):
        """Return the current selected option based on status integer."""
        if self._bypass_status == 1:
            return OPTION_BYPASS_ONCE
        elif self._bypass_status == 2:
            return OPTION_BYPASS_PERM
        return OPTION_ACTIVE

    @property
    def icon(self):
        return "mdi:shield-remove-outline" if self._bypass_status > 0 else "mdi:shield-check"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"input_{self._dev_id}")},
            name=self._attr_name,
            manufacturer="Integrated Control Technology",
            model="Protege Input",
        )

    async def async_added_to_hass(self):
        self._client.register_callback(self._handle_update)

    def _handle_update(self, update):
        if update["type"] == "input" and update["id"] == self._dev_id:
            # Check if library sends a specific 'bypass_level' integer
            # If your library only sends boolean 'bypassed', we default to OPTION_BYPASS_ONCE
            is_bypassed = update.get("bypassed", False)
            
            # Ideally, your library should send a 'bypass_level' (0,1,2).
            # Fallback logic:
            if is_bypassed:
                # If we don't know which type, assume standard bypass (1)
                # If your library supports 'bypass_level', change this line:
                # self._bypass_status = update.get("bypass_level", 1) 
                self._bypass_status = 1 
            else:
                self._bypass_status = 0
                
            self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        if option == OPTION_BYPASS_ONCE:
            # Command 1
            await self._client.send_command_with_pin(0x04, CMD_INPUT_BYPASS_ONCE, self._dev_id, None)
        elif option == OPTION_BYPASS_PERM:
            # Command 3
            await self._client.send_command_with_pin(0x04, CMD_INPUT_BYPASS_PERMANENT, self._dev_id, None)
        else:
            # Command 2 (Unbypass)
            await self._client.send_command_with_pin(0x04, CMD_INPUT_UNBYPASS, self._dev_id, None)