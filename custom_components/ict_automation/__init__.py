import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, device_registry as dr
from .const import DOMAIN, CONF_HOST, CONF_PORT, CONF_PASSWORD, CONF_DOORS, CONF_AREAS, CONF_INPUTS, CONF_OUTPUTS
from .ict_library import ICTClient

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["lock", "binary_sensor", "switch", "alarm_control_panel", "select", "button"]

async def async_setup(hass: HomeAssistant, config: dict) -> bool: return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    
    client = ICTClient(entry.data[CONF_HOST], entry.data[CONF_PORT], entry.data.get(CONF_PASSWORD))

    def get_ids(key):
        d = entry.options.get(key, {})
        if isinstance(d, dict): return [int(k) for k in d.keys()]
        return []

    door_ids = get_ids(CONF_DOORS)
    area_ids = get_ids(CONF_AREAS)
    input_ids = get_ids(CONF_INPUTS)
    output_ids = get_ids(CONF_OUTPUTS)

    client.set_configuration(
        doors=door_ids,
        areas=area_ids,
        inputs=input_ids,
        outputs=output_ids
    )
    
    await client.start()
    hass.data[DOMAIN][entry.entry_id] = client
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    
    # --- GARBAGE COLLECTOR (Entities) ---
    ent_reg = er.async_get(hass)
    valid_unique_ids = set()
    
    # 1. Build list of valid Entity IDs
    for d in door_ids:
        valid_unique_ids.add(f"ict_door_{d}")
        valid_unique_ids.add(f"ict_door_contact_{d}")
        
    for a in area_ids:
        valid_unique_ids.add(f"ict_area_{a}")

    for i in input_ids:
        valid_unique_ids.add(f"ict_input_{i}")
        valid_unique_ids.add(f"ict_input_bypass_{i}")
        valid_unique_ids.add(f"ict_trouble_{i}") 

    for o in output_ids:
        valid_unique_ids.add(f"ict_output_{o}")

    # Remove Orphaned Entities
    entries_to_remove = []
    for entity in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        if entity.unique_id not in valid_unique_ids:
            _LOGGER.warning(f"Removing orphaned entity: {entity.entity_id}")
            entries_to_remove.append(entity.entity_id)
            
    for entity_id in entries_to_remove:
        ent_reg.async_remove(entity_id)

    # --- GARBAGE COLLECTOR (Devices) ---
    # This removes the "Shell" if the device is no longer in config
    dev_reg = dr.async_get(hass)
    valid_device_identifiers = set()

    # 2. Build list of valid Device Identifiers
    for d in door_ids: valid_device_identifiers.add((DOMAIN, f"door_{d}"))
    for a in area_ids: valid_device_identifiers.add((DOMAIN, f"area_{a}"))
    for i in input_ids: valid_device_identifiers.add((DOMAIN, f"input_{i}"))
    for o in output_ids: valid_device_identifiers.add((DOMAIN, f"output_{o}"))
    # Note: Controller device is always valid
    valid_device_identifiers.add((DOMAIN, "ict_controller"))

    # Remove Orphaned Devices
    devices_to_remove = []
    for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
        # Check if ANY of the device's identifiers are in our valid list
        is_valid = False
        for identifier in device.identifiers:
            if identifier in valid_device_identifiers:
                is_valid = True
                break
        
        if not is_valid:
            _LOGGER.warning(f"Removing orphaned device: {device.name}")
            devices_to_remove.append(device.id)

    for dev_id in devices_to_remove:
        dev_reg.async_remove_device(dev_id)

    return True

async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry):
    await hass.config_entries.async_reload(entry.entry_id)

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    client = hass.data[DOMAIN][entry.entry_id]
    await client.stop()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok: hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
