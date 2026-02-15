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
    client = ICTClient(entry.data[CONF_HOST], entry.data[CONF_PORT], entry.data.get(CONF_PASSWORD))

    def get_ids(key):
        d = entry.options.get(key, {})
        if isinstance(d, dict): return [int(k) for k in d.keys()]
        if isinstance(d, str):
            ids = []
            for l in d.splitlines():
                if ":" in l: 
                    try: ids.append(int(l.split(":")[0]))
                    except: pass
            return ids
        return []

    client.set_configuration(
        doors=get_ids(CONF_DOORS),
        areas=get_ids(CONF_AREAS),
        inputs=get_ids(CONF_INPUTS),
        outputs=get_ids(CONF_OUTPUTS),
        troubles=get_ids(CONF_TROUBLES)
    )
    
    await client.start()
    hass.data[DOMAIN][entry.entry_id] = client
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    
    # Zombie Cleanup
    ent_reg = er.async_get(hass)
    entries_to_remove = []
    for entity in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        if entity.domain == "switch" and "bypass" in entity.unique_id:
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