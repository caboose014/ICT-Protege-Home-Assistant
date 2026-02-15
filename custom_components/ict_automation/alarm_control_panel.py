import logging
from homeassistant.components.alarm_control_panel import AlarmControlPanelEntity, CodeFormat, AlarmControlPanelState
from homeassistant.components.alarm_control_panel.const import AlarmControlPanelEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.entity import DeviceInfo
from .const import DOMAIN, CONF_AREAS

async def async_setup_entry(hass, entry, async_add_entities):
    client = hass.data[DOMAIN][entry.entry_id]
    data = entry.options.get(CONF_AREAS, {})
    entities = [ICTArea(client, int(k), v) for k, v in data.items()]
    async_add_entities(entities)

class ICTArea(AlarmControlPanelEntity):
    def __init__(self, client, area_id, name):
        self._client = client
        self._area_id = area_id
        self._attr_name = name
        self._attr_unique_id = f"ict_area_{area_id}"
        self._state = AlarmControlPanelState.DISARMED
        self._attr_code_format = CodeFormat.NUMBER
        self._attr_supported_features = AlarmControlPanelEntityFeature.ARM_AWAY | AlarmControlPanelEntityFeature.ARM_HOME | AlarmControlPanelEntityFeature.ARM_NIGHT | AlarmControlPanelEntityFeature.TRIGGER

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"area_{self._area_id}")},
            name=self._attr_name,
            manufacturer="Integrated Control Technology",
            model="Protege Area",
            via_device=(DOMAIN, "ict_controller"),
        )

    async def async_added_to_hass(self):
        self._client.register_callback(self._handle_update)

    @callback
    def _handle_update(self, update):
        if update["type"] == "area" and update["id"] == self._area_id:
            if update["alarm"]: self._state = AlarmControlPanelState.TRIGGERED
            elif update["armed"]: self._state = AlarmControlPanelState.ARMED_AWAY
            else: self._state = AlarmControlPanelState.DISARMED
            self.async_write_ha_state()

    @property
    def alarm_state(self): return self._state

    async def async_alarm_arm_away(self, code=None):
        if code: await self._client.send_command_with_pin(0x02, 0x03, self._area_id, code)
        else: await self._client.send_command(0x02, 0x03, self._area_id)
    
    async def async_alarm_arm_home(self, code=None):
        if code: await self._client.send_command_with_pin(0x02, 0x05, self._area_id, code)
        else: await self._client.send_command(0x02, 0x05, self._area_id)

    async def async_alarm_arm_night(self, code=None):
        if code: await self._client.send_command_with_pin(0x02, 0x06, self._area_id, code)
        else: await self._client.send_command(0x02, 0x06, self._area_id)

    async def async_alarm_disarm(self, code=None):
        if code: await self._client.send_command_with_pin(0x02, 0x00, self._area_id, code)
        else: await self._client.send_command(0x02, 0x00, self._area_id)