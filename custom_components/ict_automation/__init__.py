"""The ICT Protege integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import CONF_PIN, CONF_RECORDS
from .hub import (
    CONTROLLER_MODULE,
    ProtegeHub,
    async_name_records,
    link_child_devices,
    records_to_data,
    remove_stale_devices,
)
from .protege import ProtegeClient, ProtegeError

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.ALARM_CONTROL_PANEL,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.EVENT,
    Platform.LOCK,
    Platform.SENSOR,
    Platform.SWITCH,
]

type ProtegeConfigEntry = ConfigEntry[ProtegeHub]


async def async_setup_entry(hass: HomeAssistant, entry: ProtegeConfigEntry) -> bool:
    client = ProtegeClient(entry.data[CONF_HOST], entry.data[CONF_PORT], entry.data[CONF_PIN])
    try:
        await client.connect()
    except ProtegeError as err:
        if not entry.data.get(CONF_RECORDS):
            raise ConfigEntryNotReady(f"Cannot reach the panel: {err}") from err
        _LOGGER.warning("Panel unreachable (%s); starting with saved records and retrying", err)
    else:
        if entry.unique_id is None and client.panel.serial_str:
            hass.config_entries.async_update_entry(entry, unique_id=client.panel.serial_str)
        try:
            records = await client.discover()
        except ProtegeError as err:
            await client.disconnect()
            raise ConfigEntryNotReady(f"Discovery failed: {err}") from err
        await async_name_records(hass, entry, records)
        data = records_to_data(records)
        if data != entry.data.get(CONF_RECORDS):
            hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_RECORDS: data})

    hub = ProtegeHub(hass, entry, client)
    entry.runtime_data = hub
    await hub.async_start()
    remove_stale_devices(hass, hub)
    dev_reg = dr.async_get(hass)
    dev_reg.async_get_or_create(config_entry_id=entry.entry_id, **hub.controller_device)
    # Expansion modules hold no entities themselves; inputs and outputs link to them.
    for module in sorted({r.module for r in hub.records if r.module and r.module != CONTROLLER_MODULE}):
        dev_reg.async_get_or_create(config_entry_id=entry.entry_id, **hub.module_device(module))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    link_child_devices(hass, hub)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ProtegeConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ProtegeConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate entries made by the 1.x integration."""
    if entry.version == 1:
        # 1.x kept hand-entered records in options and used different unique IDs.
        # Start clean: keep the connection details, drop the old entities and devices.
        ent_reg = er.async_get(hass)
        for entity in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
            ent_reg.async_remove(entity.entity_id)
        dev_reg = dr.async_get(hass)
        for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
            dev_reg.async_remove_device(device.id)
        data = {
            CONF_HOST: entry.data[CONF_HOST],
            CONF_PORT: int(entry.data.get(CONF_PORT, 21000)),
            CONF_PIN: str(entry.data.get("password", "")),
        }
        hass.config_entries.async_update_entry(entry, data=data, options={}, version=2)
        _LOGGER.info("Migrated ICT Protege entry %s to version 2", entry.title)
    return True
