"""Moving a 1.x entry to version 2 without losing its entities.

1.x kept hand-entered records in the entry's options and gave entities unique IDs such as
`ict_door_1` and devices identifiers such as `door_1`, with no panel serial in them. The
config entry migration keeps those records; once the panel has been reached and the serial
and numbering are known, `async_rename_v1_registries` gives the old entities and devices the
version 2 IDs, so entity IDs, names and history carry over.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .hub import record_id_prefix
from .protege import Record, RecordType

_LOGGER = logging.getLogger(__name__)

# 1.x option keys holding {id: name} (or "id: name" lines), by record type.
V1_SECTIONS = {
    "doors_config": RecordType.DOOR,
    "areas_config": RecordType.AREA,
    "inputs_config": RecordType.INPUT,
    "outputs_config": RecordType.OUTPUT,
    "troubles_config": RecordType.TROUBLE_INPUT,
}

# 1.x entity unique ID prefix -> (record type, version 2 entity key).
_V1_ENTITIES = {
    "door": (RecordType.DOOR, "lock"),
    "door_contact": (RecordType.DOOR, "contact"),
    "area": (RecordType.AREA, "area"),
    "input": (RecordType.INPUT, "input"),
    "trouble": (RecordType.TROUBLE_INPUT, "trouble"),
    "output": (RecordType.OUTPUT, "output"),
}
_V1_ENTITY = re.compile(r"^ict_(door_contact|door|area|input|trouble|output)_(\d+)$")
# 1.x bypass selects have no version 2 counterpart (bypass is a switch now).
_V1_BYPASS = re.compile(r"^ict_(input|trouble)_bypass_\d+$")
_V1_DEVICE = re.compile(r"^(door|area|input|output|trouble)_(\d+)$")
_V1_DEVICE_TYPES = {
    "door": RecordType.DOOR,
    "area": RecordType.AREA,
    "input": RecordType.INPUT,
    "output": RecordType.OUTPUT,
    "trouble": RecordType.TROUBLE_INPUT,
}


def records_from_v1(options: dict[str, Any]) -> list[Record]:
    """The records a 1.x entry was set up with, with the names typed for them."""
    records: list[Record] = []
    for key, record_type in V1_SECTIONS.items():
        raw = options.get(key)
        if isinstance(raw, dict):
            items = list(raw.items())
        elif isinstance(raw, str):
            items = [tuple(line.split(":", 1)) for line in raw.splitlines() if ":" in line]
        else:
            continue
        seen: set[int] = set()
        for index_text, name in items:
            try:
                index = int(str(index_text).strip())
            except ValueError:
                _LOGGER.warning("Skipping 1.x %s entry %r", key, index_text)
                continue
            if index < 0 or index in seen:
                continue
            seen.add(index)
            records.append(Record(record_type, index, name=str(name or "").strip() or None))
    return records


def async_rename_v1_registries(
    hass: HomeAssistant, entry: ConfigEntry, serial: str, records: list[Record], by_record_id: bool
) -> None:
    """Give 1.x entities and devices their version 2 IDs. Ones without a record are left alone."""
    known = {r.key for r in records}
    ent_reg = er.async_get(hass)
    renamed = 0
    for entity in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        if _V1_BYPASS.match(entity.unique_id):
            ent_reg.async_remove(entity.entity_id)
            continue
        if (match := _V1_ENTITY.match(entity.unique_id)) is None:
            continue
        record_type, key = _V1_ENTITIES[match.group(1)]
        index = int(match.group(2))
        if (record_type, index) not in known:
            continue
        new_id = f"{record_id_prefix(serial, record_type, index, by_record_id)}_{key}"
        if ent_reg.async_get_entity_id(entity.domain, DOMAIN, new_id) is not None:
            continue
        changes: dict[str, Any] = {"new_unique_id": new_id}
        if record_type is RecordType.TROUBLE_INPUT:
            # Trouble inputs sit on the controller now; detach them so removing the old
            # per-trouble device does not take the entity with it.
            changes["device_id"] = None
        ent_reg.async_update_entity(entity.entity_id, **changes)
        renamed += 1

    dev_reg = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
        idents = set(device.identifiers)
        for domain, ident in device.identifiers:
            if domain != DOMAIN or (match := _V1_DEVICE.match(ident)) is None:
                continue
            record_type = _V1_DEVICE_TYPES[match.group(1)]
            index = int(match.group(2))
            if record_type is RecordType.TROUBLE_INPUT or (record_type, index) not in known:
                continue
            idents.discard((domain, ident))
            idents.add((DOMAIN, record_id_prefix(serial, record_type, index, by_record_id)))
        if idents != device.identifiers:
            dev_reg.async_update_device(device.id, new_identifiers=idents)
    _LOGGER.info("Kept %d entities from the 1.x integration", renamed)
