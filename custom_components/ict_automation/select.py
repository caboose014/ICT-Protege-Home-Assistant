import logging
from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from .const import DOMAIN, CONF_INPUTS, CONF_TROUBLES

OPTIONS = ["Unbypassed", "Temporary Bypass", "Permanent Bypass"]

async def async_setup_entry(hass, entry, async_add_entities):
    client = hass.data[DOMAIN][entry.entry_id]
    data_in = entry.options.get(CONF_INPUTS, {})
    inputs = [ICTBypassSelect(client, int(k), v, False) for k, v in data_in.items()]
    data_tr = entry.options.get(CONF_TROUBLES, {})
    troubles = [ICTBypassSelect(client, int(k), v, True) for k, v in data_tr.items()]
    async_add_entities(inputs + troubles)

class ICTBypassSelect(SelectEntity):
    def __init__(self, client, dev_id, name, is_trouble):
        self._client = client
        self._dev_id = dev_id
        self._attr_name = f"{name} Bypass"
        self._is_trouble = is_trouble
        
        if is_trouble:
            self._attr_unique_id = f"ict_trouble_bypass_{dev_id}"
            self._type_key = "trouble"
            self._id_prefix = "trouble"
            self._model = "Protege Trouble Input"
            self._group = 0x06
        else:
            self._attr_unique_id = f"ict_input_bypass_{dev_id}"
            self._type_key = "input"
            self._id_prefix = "input"
            self._model = "Protege Input"
            self._group = 0x04
            
        self._attr_current_option = OPTIONS[0]
        self._attr_options = OPTIONS
        self._attr_entity_category = EntityCategory.CONFIG

    @property
    def icon(self):
        if self._attr_current_option == "Unbypassed": return "mdi:shield-check"
        return "mdi:shield-off"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self._id_prefix}_{self._dev_id}")},
            name=self._attr_name.replace(" Bypass", ""),
            manufacturer="Integrated Control Technology",
            model=self._model,
            via_device=(DOMAIN, "ict_controller"),
        )

    async def async_added_to_hass(self):
        self._client.register_callback(self._handle_update)

    @callback
    def _handle_update(self, update):
        if update["type"] == self._type_key and update["id"] == self._dev_id:
            if "bypass_mode" in update:
                self._attr_current_option = update["bypass_mode"]
                self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        sub_cmd = 0x00
        if option == "Temporary Bypass": sub_cmd = 0x01
        elif option == "Permanent Bypass": sub_cmd = 0x02
        await self._client.send_command(self._group, sub_cmd, self._dev_id)
        self._attr_current_option = option
        self.async_write_ha_state()