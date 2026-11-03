"""The ICT Protege integration."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval

from .const import CONF_CONNECTION, CONF_NUMBERING, CONF_PIN, CONF_RECORDS, CONNECTION_WEB, DOMAIN
from .hub import (
    ProtegeHub,
    async_discover_records,
    link_child_devices,
    records_to_data,
    remove_stale_devices,
)
from .protege import ProtegeClient, ProtegeError
from .protege.web import ProtegeWebClient, WebAuthError
from .upgrade import CHECK_INTERVAL, async_check_control_service, session_store

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


def is_web(entry: ConfigEntry) -> bool:
    return entry.data.get(CONF_CONNECTION) == CONNECTION_WEB


async def _async_setup_web_client(hass: HomeAssistant, entry: ProtegeConfigEntry) -> ProtegeWebClient:
    """Web-only mode: log in (or pick up the session saved before a restart) and discover."""
    client = ProtegeWebClient(
        async_get_clientsession(hass, verify_ssl=False),
        entry.data[CONF_HOST],
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        int(entry.data[CONF_PORT]),
    )
    # Keep the session cookie across restarts, so a crash doesn't strand a session on the controller.
    store = session_store(hass, entry)
    saved = await store.async_load() or {}
    client.resume(saved.get("cookie"))

    def save_cookie(cookie: str | None) -> None:
        store.async_delay_save(lambda: {"cookie": cookie} if cookie else {}, 1)

    entry.async_on_unload(client.add_cookie_listener(save_cookie))
    try:
        await client.connect()
        records = await client.discover()
    except WebAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except ProtegeError as err:
        if not entry.data.get(CONF_RECORDS):
            raise ConfigEntryNotReady(f"Cannot reach the web interface: {err}") from err
        _LOGGER.warning("Web interface unreachable (%s); starting with saved records and retrying", err)
        return client
    if entry.unique_id is None and client.panel.serial_str:
        hass.config_entries.async_update_entry(entry, unique_id=client.panel.serial_str)
    data = records_to_data(records)
    if data != entry.data.get(CONF_RECORDS):
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_RECORDS: data})
    return client


async def async_setup_entry(hass: HomeAssistant, entry: ProtegeConfigEntry) -> bool:
    if is_web(entry):
        await _async_finish_setup(hass, entry, await _async_setup_web_client(hass, entry))
        _watch_for_control_service(hass, entry)
        return True
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
            records = await async_discover_records(hass, entry, client)
        except ProtegeError as err:
            await client.disconnect()
            raise ConfigEntryNotReady(f"Discovery failed: {err}") from err
        data = {**entry.data, CONF_RECORDS: records_to_data(records), CONF_NUMBERING: client.numbering}
        if data != entry.data:
            hass.config_entries.async_update_entry(entry, data=data)
    return await _async_finish_setup(hass, entry, client)


def _watch_for_control_service(hass: HomeAssistant, entry: ProtegeConfigEntry) -> None:
    """Web-only mode: now and then, look for the control service and offer the upgrade under Repairs.

    Once on the control service the integration stays there; it never falls back to the web.
    """

    async def check(_now: Any = None) -> None:
        await async_check_control_service(hass, entry)

    entry.async_create_background_task(hass, check(), f"{DOMAIN} control service check")
    entry.async_on_unload(async_track_time_interval(hass, check, timedelta(seconds=CHECK_INTERVAL)))


async def _async_finish_setup(
    hass: HomeAssistant, entry: ProtegeConfigEntry, client: ProtegeClient | ProtegeWebClient
) -> bool:
    hub = ProtegeHub(hass, entry, client)
    entry.runtime_data = hub
    await hub.async_start()
    remove_stale_devices(hass, hub)
    dev_reg = dr.async_get(hass)
    dev_reg.async_get_or_create(config_entry_id=entry.entry_id, **hub.controller_device)
    # Expansion modules hold no entities themselves; inputs and outputs link to them.
    for module in sorted(hub.modules):
        dev_reg.async_get_or_create(config_entry_id=entry.entry_id, **hub.module_device(module))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    link_child_devices(hass, hub)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ProtegeConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Deleting the integration also deletes its saved web session."""
    await session_store(hass, entry).async_remove()


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
