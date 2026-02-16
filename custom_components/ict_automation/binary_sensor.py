import logging
from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.entity import DeviceInfo
from .const import DOMAIN, CONF_INPUTS, CONF_DOORS

async def async_setup_entry(hass, entry, async_add_entities):
    client = hass.data[DOMAIN][entry.entry_id]
    
    # Process Inputs
    data_in = entry.options.get(CONF_INPUTS, {})
    entities = []
    for k, v in data_in.items():
        # Create standard Input Entity
        entities.append(ICTInput(client, int(k), v, "input"))
        # Automatically create Trouble Entity linked to same ID
        entities.append(ICTInput(client, int(k), v, "trouble"))
        
    # Process Doors
    data_dr = entry.options.get(CONF_DOORS, {})
    for k, v in data_dr.items():
        entities.append(ICTInput(client, int(k), v, "door"))
        
    async_add_entities(entities)

class ICTInput(BinarySensorEntity):
    def __init__(self, client, dev_id, name, sensor_type):
        self._client = client
        self._dev_id = dev_id
        self._type = sensor_type
        
        if sensor_type == "trouble":
            self._attr_name = f"{name} Trouble"
            self._attr_unique_id = f"ict_trouble_{dev_id}"
            self._attr_device_class = BinarySensorDeviceClass.PROBLEM
            self._model = "Protege Input"
            # Identifiers match the Input Device ID
            self._device_id_prefix = "input" 
        elif sensor_type == "door":
            self._attr_name = f"{name} Contact"
            self._attr_unique_id = f"ict_door_contact_{dev_id}"
            self._attr_device_class = BinarySensorDeviceClass.DOOR
            self._model = "Protege Door"
            self._device_id_prefix = "door"
        else:
            self._attr_name = name
            self._attr_unique_id = f"ict_input_{dev_id}"
            self._attr_device_class = None
            self._model = "Protege Input"
            self._device_id_prefix = "input"
            
        self._is_on = False
        self._attr_extra_state_attributes = {}

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
