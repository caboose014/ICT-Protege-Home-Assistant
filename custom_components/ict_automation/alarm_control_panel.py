import logging
from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    CodeFormat,
)

# FIXED: Constants defined locally to prevent ImportError on HA 2025.1+
# These were removed from homeassistant.const
STATE_ALARM_DISARMED = "disarmed"
STATE_ALARM_ARMED_HOME = "armed_home"
STATE_ALARM_ARMED_AWAY = "armed_away"
STATE_ALARM_ARMED_NIGHT = "armed_night"
STATE_ALARM_TRIGGERED = "triggered"
STATE_ALARM_ARMING = "arming"

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
            model="Protege Area",
            via_device=(DOMAIN, "ict_controller"),
        )

    async def async_added_to_hass(self):
        self._client.register_callback(self._handle_update)

    @callback
    def _handle_update(self, update):
        if update["type"] == "area" and update["id"] == self._area_id:
            if update["alarm"]: self._state = STATE_ALARM_TRIGGERED
            elif update["armed"]: self._state = STATE_ALARM_ARMED_AWAY
            else: self._state = STATE_ALARM_DISARMED
            self.async_write_ha_state()

    @property
    def state(self): return self._state

    async def async_alarm_disarm(self, code=None) -> None:
        if not code: return
        await self._client.send_command_with_pin(0x02, 0x02, self._area_id, code)

    async def async_alarm_arm_away(self, code=None) -> None:
        if not code: return
        # Standard Force Arm
        await self._client.send_command_with_pin(0x02, 0x01, self._area_id, code)

    async def async_alarm_arm_home(self, code=None) -> None:
        if not code: return
        # Stay Arm (Protege "Stay" Mode)
        await self._client.send_command_with_pin(0x02, 0x03, self._area_id, code)

    async def async_alarm_arm_night(self, code=None) -> None:
        if not code: return
        # Night Arm (Protege "Instant" Mode usually maps well here, or Sleep)
        # Using 0x04 (Instant/Sleep) based on standard automation protocols
        await self._client.send_command_with_pin(0x02, 0x04, self._area_id, code)
        
    async def async_alarm_arm_vacation(self, code=None) -> None:
        # We use this for "Force Arm" or specific bypass modes if enabled
        if not code: return
        await self._client.send_command_with_pin(0x02, 0x01, self._area_id, code)
