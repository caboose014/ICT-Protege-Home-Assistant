import logging
from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
    CodeFormat,
)

from homeassistant.core import callback
from homeassistant.helpers.entity import DeviceInfo
from .const import (
    DOMAIN, CONF_AREAS, 
    CONF_ENABLE_AWAY, CONF_ENABLE_STAY, CONF_ENABLE_NIGHT, CONF_ENABLE_BYPASS
)

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    client = hass.data[DOMAIN][entry.entry_id]
    data = entry.options.get(CONF_AREAS, {})
    
    # Read User Preferences for Arming Modes (Default to True if not set)
    enable_away = entry.options.get(CONF_ENABLE_AWAY, True)
    enable_stay = entry.options.get(CONF_ENABLE_STAY, True)
    enable_night = entry.options.get(CONF_ENABLE_NIGHT, True)
    enable_bypass = entry.options.get(CONF_ENABLE_BYPASS, False)

    async_add_entities([
        ICTArea(client, int(k), v, enable_away, enable_stay, enable_night, enable_bypass) 
        for k, v in data.items()
    ])

class ICTArea(AlarmControlPanelEntity):
    def __init__(self, client, area_id, name, enable_away, enable_stay, enable_night, enable_bypass):
        self._client = client
        self._area_id = area_id
        self._attr_name = name
        self._attr_unique_id = f"ict_area_{area_id}"
        self._attr_code_format = CodeFormat.NUMBER
        self._state = None
        
        # Build Supported Features based on Config
        features = AlarmControlPanelEntityFeature(0)
        
        if enable_away: features |= AlarmControlPanelEntityFeature.ARM_AWAY
        if enable_stay: features |= AlarmControlPanelEntityFeature.ARM_HOME
        if enable_night: features |= AlarmControlPanelEntityFeature.ARM_NIGHT
        if enable_bypass: features |= AlarmControlPanelEntityFeature.ARM_VACATION # We map Bypass to Vacation for HA compatibility

        features |= AlarmControlPanelEntityFeature.TRIGGER
        self._attr_supported_features = features

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"area_{self._area_id}")},
            name=self._attr_name,
            manufacturer="Integrated Control Technology",
            model="Protege Area"
        )

    async def async_added_to_hass(self):
        self._client.register_callback(self._handle_update)

    @callback
    def _handle_update(self, update):
        if update["type"] == "area" and update["id"] == self._area_id:
            area_state = update.get("state")
            if update["alarm"]:
                self._state = AlarmControlPanelState.TRIGGERED
            elif area_state == 0x81:
                self._state = AlarmControlPanelState.ARMING
            elif area_state in (0x82, 0x83, 0x84):
                self._state = AlarmControlPanelState.PENDING
            elif update.get("partial_armed"):
                self._state = AlarmControlPanelState.ARMED_HOME
            elif update.get("instant_armed"):
                self._state = AlarmControlPanelState.ARMED_NIGHT
            elif update["armed"]:
                self._state = AlarmControlPanelState.ARMED_AWAY
            else:
                self._state = AlarmControlPanelState.DISARMED
            self._attr_extra_state_attributes = {
                "ict_status": update.get("status"),
                "force_armed": update.get("force_armed"),
                "instant_armed": update.get("instant_armed"),
                "partial_armed": update.get("partial_armed"),
                "alarm_memory": update.get("alarm_memory"),
                "siren": update.get("siren"),
                "tamper_state": update.get("tamper_state"),
            }
            self.async_write_ha_state()

    @property
    def state(self): return self._state

    @property
    def alarm_state(self): return self._state

    async def async_alarm_disarm(self, code=None) -> None:
        if not code: return
        await self._client.disarm_area(self._area_id, code)

    async def async_alarm_arm_away(self, code=None) -> None:
        if not code: return
        await self._client.arm_area_away(self._area_id, code)

    async def async_alarm_arm_home(self, code=None) -> None:
        if not code: return
        await self._client.arm_area_stay(self._area_id, code)

    async def async_alarm_arm_night(self, code=None) -> None:
        if not code: return
        await self._client.arm_area_instant(self._area_id, code)
        
    async def async_alarm_arm_vacation(self, code=None) -> None:
        if not code: return
        await self._client.force_arm_area(self._area_id, code)
