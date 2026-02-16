import logging
from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorDeviceClass,
)
from homeassistant.helpers.entity import DeviceInfo
from .const import DOMAIN, CONF_DOORS, CONF_INPUTS

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    client = hass.data[DOMAIN][entry.entry_id]
    
    entities = []

    # 1. Setup Doors (Contact Sensors)
    # These belong to the "Door" device group
    doors_data = entry.options.get(CONF_DOORS, {})
    for k, v in doors_data.items():
        name = v.get("name", str(v)) if isinstance(v, dict) else str(v)
        # Main Door Contact
        entities.append(ICTInput(client, int(k), name, "door"))

    # 2. Setup Inputs (PIR, Smoke, etc)
    inputs_data = entry.options.get(CONF_INPUTS, {})
    for k, v in inputs_data.items():
        dev_id = int(k)
        
        # Handle Legacy vs New Config
        if isinstance(v, dict):
            name = v.get("name", f"Input {dev_id}")
            sensor_type = v.get("type", "motion")
        else:
            name = str(v)
            sensor_type = "motion"
            
        # Main Sensor (The Motion/Smoke detector itself)
        # For standard inputs, the prefix is "input"
        entities.append(ICTInput(client, dev_id, name, sensor_type))
        
        # Trouble Sensor (The Tamper/Trouble status)
        # We pass the SAME sensor_type so it knows how to group with the parent
        entities.append(ICTInput(client, dev_id, name, "trouble", parent_type=sensor_type))
        
    async_add_entities(entities)

class ICTInput(BinarySensorEntity):
    def __init__(self, client, dev_id, name, sensor_type, parent_type=None):
        self._client = client
        self._dev_id = dev_id
        self._type = sensor_type
        
        # Determine Device Grouping
        # If this is a "trouble" sensor, it needs to group with its parent (e.g., the Door)
        # If parent_type is "door", use "door" prefix. Otherwise default to "input".
        effective_type = parent_type if sensor_type == "trouble" and parent_type else sensor_type
        
        if effective_type == "door":
            self._device_id_prefix = "door"
            self._model = "Protege Door"
        elif effective_type == "smoke":
            self._device_id_prefix = "smoke"
            self._model = "Protege Smoke Detector"
        elif effective_type == "window":
            self._device_id_prefix = "window"
            self._model = "Protege Window"
        elif effective_type == "tamper":
            self._device_id_prefix = "tamper"
            self._model = "Protege Tamper"
        else:
            self._device_id_prefix = "input"
            self._model = "Protege Input"

        # --- CONFIGURE ENTITY ---
        if sensor_type == "smoke":
            self._attr_name = f"{name} Smoke"
            self._attr_unique_id = f"ict_smoke_{dev_id}"
            self._attr_device_class = BinarySensorDeviceClass.SMOKE
            
        elif sensor_type == "door":
            self._attr_name = f"{name} Contact"
            self._attr_unique_id = f"ict_door_contact_{dev_id}"
            self._attr_device_class = BinarySensorDeviceClass.DOOR
            
        elif sensor_type == "window":
            self._attr_name = f"{name} Window"
            self._attr_unique_id = f"ict_window_contact_{dev_id}"
            self._attr_device_class = BinarySensorDeviceClass.WINDOW

        elif sensor_type == "tamper":
            self._attr_name = f"{name} Tamper"
            self._attr_unique_id = f"ict_tamper_{dev_id}"
            self._attr_device_class = BinarySensorDeviceClass.TAMPER

        elif sensor_type == "trouble":
            self._attr_name = f"{name} Trouble"
            self._attr_unique_id = f"ict_trouble_{dev_id}"
            self._attr_device_class = BinarySensorDeviceClass.PROBLEM
            # IMPORTANT: Unique ID for trouble must be distinct, but Device Info matches parent
            
        else:
            # Default = Motion
            self._attr_name = name
            self._attr_unique_id = f"ict_input_{dev_id}"
            self._attr_device_class = BinarySensorDeviceClass.MOTION

        # --- DEVICE INFO ---
        # This links the entity to the physical device registry
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{self._device_id_prefix}_{dev_id}")},
            name=name,
            manufacturer="Integrated Control Technology",
            model=self._model,
            # Removed via_device to prevent "Invalid Device