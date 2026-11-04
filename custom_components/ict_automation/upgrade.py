"""Moving a web-only install onto the Automation and Control service, keeping its entities.

Web-only mode numbers records by their WX record IDs. The control service uses the same
IDs by default, so nothing changes. With ACPUseDisplayOrder it numbers them 1, 2, 3 in
display order instead; the WX lists come back in that same order (as for names), so
records are matched by position within each type and the entity and device registries
are renamed to match.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store

from .const import (
    CONF_CONNECTION,
    CONF_CONTROL_PORT,
    CONF_NUMBERING,
    CONF_PIN,
    CONF_RECORDS,
    CONF_WX_PASSWORD,
    CONF_WX_PORT,
    CONF_WX_USERNAME,
    CONNECTION_ACS,
    DEFAULT_PORT,
    DOMAIN,
)
from .hub import record_id_prefix, records_from_data, records_to_data
from .protege import ProtegeClient, ProtegeError, Record, RecordType
from .protege.client import NUMBERING_RECORD_ID

_LOGGER = logging.getLogger(__name__)

CHECK_INTERVAL = 6 * 3600  # seconds between checks for the control service in web-only mode


def session_store(hass: HomeAssistant, entry: ConfigEntry) -> Store[dict[str, str]]:
    """Web-only mode's saved session cookie (only readable by the user Home Assistant runs as)."""
    return Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.session", private=True)


def issue_id(entry: ConfigEntry) -> str:
    return f"acs_available_{entry.entry_id}"


def control_port(entry: ConfigEntry) -> int:
    return int(entry.data.get(CONF_CONTROL_PORT) or DEFAULT_PORT)


class WrongPanel(ProtegeError):
    """The control service belongs to a different controller."""


async def control_service_answers(host: str, port: int) -> bool:
    """Whether an Automation and Control service answers on this port (no login needed)."""
    client = ProtegeClient(host, port, "")
    try:
        await client.connect()
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("No Automation and Control service on %s:%s: %s", host, port, err)
        return False
    await client.disconnect()
    return True


async def async_check_control_service(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Look for the control service, and offer the upgrade under Repairs while it answers."""
    port = control_port(entry)
    found = await control_service_answers(entry.data[CONF_HOST], port)
    if found:
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id(entry),
            is_fixable=True,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="acs_available",
            translation_placeholders={"port": str(port), "title": entry.title},
            data={"entry_id": entry.entry_id},
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, issue_id(entry))
    return found


def index_map(web: list[Record], acs: list[Record]) -> dict[tuple[RecordType, int], int]:
    """WX id → control service index, by position within each type (only where the counts agree)."""
    by_type_web: dict[RecordType, list[Record]] = defaultdict(list)
    by_type_acs: dict[RecordType, list[Record]] = defaultdict(list)
    for r in web:
        by_type_web[r.record_type].append(r)
    for r in sorted(acs, key=lambda r: r.index):
        by_type_acs[r.record_type].append(r)
    mapping: dict[tuple[RecordType, int], int] = {}
    for record_type, items in by_type_web.items():
        theirs = by_type_acs.get(record_type, [])
        if len(items) != len(theirs):
            _LOGGER.warning(
                "Not keeping %s entities: the web interface listed %d but the control service reports %d",
                record_type.value,
                len(items),
                len(theirs),
            )
            continue
        for w, a in zip(items, theirs):
            mapping[(record_type, w.index)] = a.index
    return mapping


def _renumber(serial: str, mapping: dict[tuple[RecordType, int], int], by_record_id: bool) -> dict[str, str]:
    """Old record prefix → new prefix, e.g. 'SER_input_id14' → 'SER_input_2' (display order)."""
    renames = {
        record_id_prefix(serial, record_type, old, True): record_id_prefix(
            serial, record_type, new, by_record_id
        )
        for (record_type, old), new in mapping.items()
    }
    return {old: new for old, new in renames.items() if old != new}


def _rename(old_id: str, prefixes: dict[str, str]) -> str | None:
    for old, new in prefixes.items():
        if old_id == old:
            return new
        if old_id.startswith(old + "_"):
            return new + old_id[len(old) :]
    return None


def _migrate_registries(hass: HomeAssistant, entry: ConfigEntry, prefixes: dict[str, str]) -> None:
    """Rename in two passes so swapped numbers never collide."""
    ent_reg = er.async_get(hass)
    entities = [
        (e.entity_id, new)
        for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
        if (new := _rename(e.unique_id, prefixes)) is not None
    ]
    for entity_id, new in entities:
        ent_reg.async_update_entity(entity_id, new_unique_id=f"__upgrading__{new}")
    for entity_id, new in entities:
        ent_reg.async_update_entity(entity_id, new_unique_id=new)

    dev_reg = dr.async_get(hass)
    devices = []
    for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
        idents = {(d, _rename(i, prefixes) or i) if d == DOMAIN else (d, i) for d, i in device.identifiers}
        if idents != device.identifiers:
            devices.append((device.id, idents))
    for device_id, idents in devices:
        dev_reg.async_update_device(device_id, new_identifiers={(d, f"__upgrading__{i}") for d, i in idents})
    for device_id, idents in devices:
        dev_reg.async_update_device(device_id, new_identifiers=idents)


async def async_upgrade(hass: HomeAssistant, entry: ConfigEntry, port: int, pin: str) -> None:
    """Switch a web-only entry to the control service. Raises ProtegeError (InvalidPin etc.)."""
    client = ProtegeClient(entry.data[CONF_HOST], port, pin)
    info = await client.connect()
    try:
        await client.verify_pin()
        web_records = records_from_data(entry.data.get(CONF_RECORDS, []))
        listed: dict[RecordType, list[int]] = defaultdict(list)
        for r in web_records:
            listed[r.record_type].append(r.index)
        records = await client.discover(listed=dict(listed))
    finally:
        await client.disconnect()
    if entry.unique_id and info.serial_str and info.serial_str != entry.unique_id:
        raise WrongPanel(f"control service reports serial {info.serial_str}, expected {entry.unique_id}")

    if client.numbering == NUMBERING_RECORD_ID:
        # Both sides use the same record IDs: nothing to renumber.
        mapping = {r.key: r.index for r in web_records}
    else:
        mapping = index_map(web_records, records)
    # Carry names over now; the WX read at start-up refreshes them and the zone settings.
    names = {mapping[r.key]: r.name for r in web_records if r.key in mapping and r.name}
    by_new = {(rt, new): names.get(new) for (rt, _), new in mapping.items()}
    for record in records:
        record.name = by_new.get(record.key) or record.name

    serial = entry.unique_id or info.serial_str or entry.entry_id
    await hass.config_entries.async_unload(entry.entry_id)
    by_record_id = client.numbering == NUMBERING_RECORD_ID
    _migrate_registries(hass, entry, _renumber(serial, mapping, by_record_id))
    await session_store(hass, entry).async_remove()
    ir.async_delete_issue(hass, DOMAIN, issue_id(entry))

    options: dict[str, Any] = {
        CONF_WX_USERNAME: entry.data[CONF_USERNAME],
        CONF_WX_PASSWORD: entry.data[CONF_PASSWORD],
        CONF_WX_PORT: int(entry.data[CONF_PORT]),
    }
    hass.config_entries.async_update_entry(
        entry,
        title=f"Protege {info.firmware_type or 'WX'} ({entry.data[CONF_HOST]})",
        data={
            CONF_HOST: entry.data[CONF_HOST],
            CONF_PORT: port,
            CONF_PIN: pin,
            CONF_CONNECTION: CONNECTION_ACS,
            CONF_NUMBERING: client.numbering,
            CONF_RECORDS: records_to_data(records),
        },
        options=options,
    )
    await hass.config_entries.async_setup(entry.entry_id)
