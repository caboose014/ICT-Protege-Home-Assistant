import logging
from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.entity import DeviceInfo
from .const import DOMAIN, CONF_INPUTS, CONF_DOORS

async def async_setup_entry(hass, entry, async_add_entities):
    client = hass.data[DOMAIN][entry.entry_id]
    
    # 1. Setup Standard Doors (Contact Sensors)
    doors_data = entry.options.get(CONF_DOORS, {})
    door_sensors = []
    for k, v in doors_data.items():
        # Doors are always type="door"
        door_sensors.append(ICTInput(client, int(k), v, "door"))
    async_add_entities(door_sensors)

    # 2. Setup Configurable Inputs
    inputs_data = entry.options.get(CONF_INPUTS, {})
    input_sensors = []
    
    for k, v in inputs_data.items():
        dev_id = int(k)
        
        # Handle Legacy Config (String) vs New Config (Dict)
        if isinstance(v, dict):
            name = v.get("name", f"Input {dev_id}")
            sensor_type = v.get("type", "motion") # Default to Motion
        else:
            name = str(v)
            sensor_type = "motion" # Legacy fallback
            
        # Add the Main Sensor (Motion, Smoke, etc.)
        input_sensors.append(ICTInput(client, dev_id, name, sensor_type))
        
        # Add the Hidden Trouble Sensor (Always type="trouble")
        input_sensors.append(ICTInput(client, dev_id, name, "trouble"))
        
    async_add_entities(input_sensors)

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorDeviceClass,
)

class ICTInput(BinarySensorEntity):
    def __init__(self, client, dev_id, name, sensor_type):
        self._client = client
        self._dev_id = dev_id
        self._type = sensor_type
        
        # --- SMOKE DETECTORS ---
        if sensor_type == "smoke":
            self._attr_name = f"{name} Smoke"
            self._attr_unique_id = f"ict_smoke_{dev_id}"
            # Feedback: "Smoke Detected" / "Clear"
            self._attr_device_class = BinarySensorDeviceClass.SMOKE
            self._model = "Protege Smoke Detector"
            self._device_id_prefix = "smoke"

        # --- DOORS (Reed Switches) ---
        elif sensor_type == "door":
            self._attr_name = f"{name} Contact"
            self._attr_unique_id = f"ict_door_contact_{dev_id}"
            # Feedback: "Open" / "Closed"
            self._attr_device_class = BinarySensorDeviceClass.DOOR
            self._model = "Protege Door"
            self._device_id_prefix = "door"

        # --- WINDOWS (Reed Switches) ---
        elif sensor_type == "window":
            self._attr_name = f"{name} Window"
            self._attr_unique_id = f"ict_window_contact_{dev_id}"
            # Feedback: "Open" / "Closed"
            self._attr_device_class = BinarySensorDeviceClass.WINDOW
            self._model = "Protege Window"
            self._device_id_prefix = "window"

        # --- TAMPER (Box/Device Tamper) ---
        elif sensor_type == "tamper":
            self._attr_name = f"{name} Tamper"
            self._attr_unique_id = f"ict_tamper_{dev_id}"
            # Feedback: "Tampering Detected" / "OK"
            self._attr_device_class = BinarySensorDeviceClass.TAMPER
            self._model = "Protege Tamper"
            self._device_id_prefix = "tamper"

        # --- TROUBLES (System Troubles) ---
        elif sensor_type == "trouble":
            self._attr_name = f"{name} Trouble"
            self._attr_unique_id = f"ict_trouble_{dev_id}"
            # Feedback: "Problem" / "OK"
            self._attr_device_class = BinarySensorDeviceClass.PROBLEM
            self._model = "Protege Input"
            self._device_id_prefix = "input" 
            
        # --- DEFAULT (PIR / Motion) ---
        else:
            self._attr_name = name
            self._attr_unique_id = f"ict_input_{dev_id}"
            # Feedback: "Detected" / "Clear"
            self._attr_device_class = BinarySensorDeviceClass.MOTION 
            self._model = "Protege Input"
            self._device_id_prefix = "input"

    @property
    def icon(self):
        """Return the icon to use in the frontend."""
        # Icons are optional if you use device_class, but this forces specific ones if you want
        if self._type == "smoke":
            return "mdi:smoke-detector-alert" if self.is_on else "mdi:smoke-detector"
        if self._type == "window":
            return "mdi:window-open" if self.is_on else "mdi:window-closed"
        if self._type == "tamper":
            return "mdi:alert-box" if self.is_on else "mdi:check-circle-outline"
        
        # Let Home Assistant handle the default icons for Door/Motion/Problem
        return None

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self._device_id_prefix}_{self._dev_id}")},
            name=self._attr_name.replace(" Trouble", "").replace(" Contact", ""),
            manufacturer="Integrated Control Technology",
            model=self._model,
            via_device=(DOMAIN, "ict_controller"),
        )

    async def async_added_to_hass(self):
        self._client.register_callback(self._handle_update)

    @callback
    def _handle_update(self, update):
        if update["type"] == self._type and update["id"] == self._dev_id:
            if self._type == "door": self._is_on = update["open"]
            else:
                self._is_on = update["on"]
                if "status" in update: self._attr_extra_state_attributes["status_text"] = update["status"]
            self.async_write_ha_state()

    @property
    def is_on(self): return self._is_on
