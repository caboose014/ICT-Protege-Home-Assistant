import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from .const import DOMAIN, CONF_HOST, CONF_PORT, CONF_PASSWORD, CONF_DOORS, CONF_AREAS, CONF_INPUTS, CONF_OUTPUTS, CONF_TROUBLES
from .ict_library import ICTClient

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["lock", "binary_sensor", "switch", "alarm_control_panel", "select"]

async def async_setup(hass: HomeAssistant, config: dict) -> bool: return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    
    # 1. Setup Client
    client = ICTClient(entry.data[CONF_HOST], entry.data[CONF_PORT], entry.data.get(CONF_PASSWORD))

    def get_ids(key):
        d = entry.options.get(key, {})
        if isinstance(d, dict): return [int(k) for k in d.keys()]
        return []

    door_ids = get_ids(CONF_DOORS)
    area_ids = get_ids(CONF_AREAS)
    input_ids = get_ids(CONF_INPUTS)
    output_ids = get_ids(CONF_OUTPUTS)
    trouble_ids = get_ids(CONF_TROUBLES)

    client.set_configuration(
        doors=door_ids,
        areas=area_ids,
        inputs=input_ids,
        outputs=output_ids,
        troubles=trouble_ids
    )
    
    await client.start()
    hass.data[DOMAIN][entry.entry_id] = client
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    
    # 2. GARBAGE COLLECTOR (Orphan Removal)
    # This runs on every boot/reload to ensure the Entity Registry matches the Config
    ent_reg = er.async_get(hass)
    
    # Build list of ALL valid Unique IDs based on current config
    valid_unique_ids = set()
    
    for d in door_ids:
        valid_unique_ids.add(f"ict_door_{d}")          # Lock
        valid_unique_ids.add(f"ict_door_contact_{d}")  # Binary Sensor
        
    for a in area_ids:
        valid_unique_ids.add(f"ict_area_{a}")          # Alarm Panel

    for i in input_ids:
        valid_unique_ids.add(f"ict_input_{i}")         # Binary Sensor
        valid_unique_ids.add(f"ict_input_bypass_{i}")  # Select

    for t in trouble_ids:
        valid_unique_ids.add(f"ict_trouble_{t}")        # Binary Sensor
        valid_unique_ids.add(f"ict_trouble_bypass_{t}") # Select

    for o in output_ids:
        valid_unique_ids.add(f"ict_output_{o}")         # Switch

    # Scan Registry and Remove Orphans
    entries_to_remove = []
    for entity in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        if entity.unique_id not in valid_unique_ids:
            _LOGGER.warning(f"Removing orphaned entity: {entity.entity_id} (ID: {entity.unique_id})")
            entries_to_remove.append(entity.entity_id)
            
    for entity_id in entries_to_remove:
        ent_reg.async_remove(entity_id)

    return True

async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry):
    await hass.config_entries.async_reload(entry.entry_id)

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    client = hass.data[DOMAIN][entry.entry_id]
    await client.stop()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok: hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
