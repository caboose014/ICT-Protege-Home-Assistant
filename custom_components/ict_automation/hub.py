"""Hub that owns the panel connections and routes updates to entities."""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo

from .const import (
    CONF_CONNECTION,
    CONF_EVENTS_PIN,
    CONF_EVENTS_PORT,
    CONF_NUMBERING,
    CONF_PIN,
    CONF_RECORDS,
    CONF_WX_PASSWORD,
    CONF_WX_PORT,
    CONF_WX_USERNAME,
    CONNECTION_WEB,
    DEFAULT_WX_PORT,
    DOMAIN,
    EVENT_EMERGENCY,
    EVENT_PANEL,
    MANUFACTURER,
)
from .protege import (
    AreaStatus,
    InputStatus,
    PanelEvent,
    PanelInfo,
    ProtegeClient,
    ProtegeEventClient,
    Record,
    RecordType,
    StatusUpdate,
)
from .protege.client import NUMBERING_RECORD_ID
from .protege.wx import NO_RECORD, WXClient, WXError, WXSite, apply_names, apply_zone_settings

_LOGGER = logging.getLogger(__name__)

CONTROLLER_MODULE = "CP001"

# An input that opened this long before an alarm is taken as its cause.
ALARM_CAUSE_WINDOW = 90.0

# Whole words, so a name like "Panic Room" only trips it as part of a real alarm line.
_EMERGENCY = re.compile(r"\b(duress|panic|medical|hold[ -]?up)\b", re.IGNORECASE)


def emergency_kind(text: str | None) -> str | None:
    """ "duress", "medical" or "panic" when an event reports one of those alarms."""
    match = _EMERGENCY.search(text or "")
    if match is None:
        return None
    word = match.group(1).lower()
    return word if word in ("duress", "medical") else "panic"


def records_to_data(records: list[Record]) -> list[dict[str, Any]]:
    out = []
    for r in records:
        item: dict[str, Any] = {"type": r.record_type.value, "index": r.index, "reference": r.reference}
        if r.name:
            item["name"] = r.name
        if r.areas is not None:
            item["areas"] = list(r.areas)
        if r.arm_open is not None:
            item["arm_open"] = list(r.arm_open)
        out.append(item)
    return out


def records_from_data(data: list[dict[str, Any]]) -> list[Record]:
    return [
        Record(
            RecordType(d["type"]),
            int(d["index"]),
            d.get("reference"),
            d.get("name"),
            d.get("areas"),
            d.get("arm_open"),
        )
        for d in data
    ]


async def async_discover_records(
    hass: HomeAssistant, entry: ConfigEntry, client: ProtegeClient
) -> list[Record]:
    """Discover records over the control service, named and with zone settings.

    When an operator login is configured, the web interface is read first (one login):
    its record IDs tell discovery which records to read, and give names and zone settings.
    Names saved from the last successful read are used when it can't be reached.
    """
    site = await _async_read_site(hass, entry)
    saved = {r.key: r for r in records_from_data(entry.data.get(CONF_RECORDS, []))}
    listed: dict[RecordType, list[int]] = {}
    if site is not None:
        for record_type, items in site.lists.items():
            listed[record_type] = [rec_id for rec_id, _ in items if rec_id not in NO_RECORD]
    else:
        # Without the web interface, the records found last time still tell how the panel numbers them.
        for record_type, index in saved:
            listed.setdefault(record_type, []).append(index)
    records = await client.discover(listed=listed or None)
    for record in records:
        if (old := saved.get(record.key)) is not None:
            record.name = record.name or old.name
            record.areas = old.areas if record.areas is None else record.areas
            record.arm_open = old.arm_open if record.arm_open is None else record.arm_open
    if site is not None:
        by_id = client.numbering == NUMBERING_RECORD_ID
        named = apply_names(records, site.lists, by_id=by_id)
        zoned = apply_zone_settings(records, site, by_id=by_id)
        _LOGGER.debug(
            "Panel numbers records by %s; named %d records and read zone settings for %d inputs",
            client.numbering,
            named,
            zoned,
        )
    return records


async def _async_read_site(hass: HomeAssistant, entry: ConfigEntry) -> WXSite | None:
    username = entry.options.get(CONF_WX_USERNAME)
    password = entry.options.get(CONF_WX_PASSWORD)
    if not username or not password:
        return None
    client = WXClient(
        async_get_clientsession(hass, verify_ssl=False),
        entry.data[CONF_HOST],
        username,
        password,
        int(entry.options.get(CONF_WX_PORT, DEFAULT_WX_PORT)),
    )
    try:
        return await client.fetch_site()
    except WXError as err:
        _LOGGER.warning("Could not read names from the WX web interface: %s", err)
        return None


def record_id_prefix(serial: str, record_type: RecordType, index: int, by_record_id: bool) -> str:
    return f"{serial}_{record_type.value}_{'id' if by_record_id else ''}{index}"


class ProtegeHub:
    """Connection owner for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: ProtegeClient) -> None:
        self.hass = hass
        self.entry = entry
        self.client = client
        self.events: ProtegeEventClient | None = None
        self.records: list[Record] = records_from_data(entry.data.get(CONF_RECORDS, []))
        self._listeners: dict[tuple[RecordType, int], list[CALLBACK_TYPE]] = defaultdict(list)
        self._availability_listeners: list[CALLBACK_TYPE] = []
        self._event_listeners: list[Callable[[PanelEvent], None]] = []
        self._unsubs: list[CALLBACK_TYPE] = []
        # Arm modes turned off per area index, set by each area's arm mode switches.
        self.disabled_arm_modes: dict[int, set[str]] = defaultdict(set)
        # When each input last went from closed to active (monotonic seconds).
        self.last_active: dict[int, float] = {}
        self._input_active: dict[int, bool] = {}
        self._area_alarm: dict[int, bool] = {}
        # The input that set each area's alarm off, kept until the area is disarmed.
        self.alarm_cause: dict[int, str | None] = {}

    # ----- identity -------------------------------------------------------

    @property
    def web(self) -> bool:
        """Web-only mode (no Automation and Control service)."""
        return self.entry.data.get(CONF_CONNECTION) == CONNECTION_WEB

    @property
    def has_events(self) -> bool:
        return self.web or self.events is not None

    @property
    def panel(self) -> PanelInfo:
        return self.client.panel

    @property
    def serial(self) -> str:
        return self.entry.unique_id or self.panel.serial_str or self.entry.entry_id

    @property
    def available(self) -> bool:
        return self.client.available

    @property
    def controller_device(self) -> DeviceInfo:
        fw_type = self.panel.firmware_type or "WX"
        return DeviceInfo(
            identifiers={(DOMAIN, self.serial)},
            name=f"Protege {fw_type}",
            manufacturer=MANUFACTURER,
            model=f"Protege {fw_type} controller",
            serial_number=self.panel.serial_str,
            sw_version=self.panel.firmware,
            configuration_url=f"https://{self.client.host}"
            + (f":{self.client.port}" if self.web and self.client.port != 443 else ""),
        )

    def module_device(self, module: str | None) -> DeviceInfo:
        """Device for the hardware module a record is wired to."""
        if module is None or module == CONTROLLER_MODULE:
            return self.controller_device
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.serial}_module_{module}")},
            name=module,
            manufacturer=MANUFACTURER,
            model=f"{module[:2]} module",
        )

    def record_device(self, record: Record) -> DeviceInfo:
        """Doors, areas, inputs and outputs each get their own device, so each can sit in its own HA area."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.record_device_id(record))},
            name=record.default_name,
            manufacturer=MANUFACTURER,
            model=f"Protege {record.record_type.value}",
        )

    @property
    def by_record_id(self) -> bool:
        """Whether records are numbered by record ID (web interface, or the control service without
        ACPUseDisplayOrder) rather than 1, 2, 3 in display order."""
        return self.web or self.entry.data.get(CONF_NUMBERING) == NUMBERING_RECORD_ID

    def record_device_id(self, record: Record) -> str:
        """Also the prefix of the record's entity unique IDs.

        Record IDs get an "id" marker, so a record numbered one way can never take over
        the entity of a different record numbered the other way (door 1 by display order is
        not door ID 1).
        """
        return record_id_prefix(self.serial, record.record_type, record.index, self.by_record_id)

    @property
    def modules(self) -> set[str]:
        """Expansion modules with inputs or outputs (trouble inputs sit on the controller)."""
        return {
            r.module
            for r in self.records
            if r.module
            and r.module != CONTROLLER_MODULE
            and r.record_type in (RecordType.INPUT, RecordType.OUTPUT)
        }

    def records_of(self, record_type: RecordType) -> list[Record]:
        return [r for r in self.records if r.record_type is record_type]

    # ----- zones ----------------------------------------------------------

    @property
    def zones_known(self) -> bool:
        """True once the inputs' areas have been read from the web interface."""
        return any(r.areas is not None for r in self.records_of(RecordType.INPUT))

    def area_inputs(self, area_index: int) -> list[Record]:
        return [r for r in self.records_of(RecordType.INPUT) if area_index in (r.areas or ())]

    def open_zones(self, area_index: int) -> list[str] | None:
        """Names of the open, unbypassed inputs that stop this area arming (None if unknown).

        Inputs whose type lets them stay open when arming are left out.
        """
        names = []
        for record in self.area_inputs(area_index):
            if area_index in (record.arm_open or ()):
                continue
            status = self.client.status.get(record.key)
            if not isinstance(status, InputStatus):
                return None
            if status.active and not status.bypassed:
                names.append(record.default_name)
        return names

    def _find_alarm_cause(self, area_index: int) -> str | None:
        """The input that most recently went active in the area, else one active now."""
        inputs = self.area_inputs(area_index) if self.zones_known else self.records_of(RecordType.INPUT)
        cutoff = time.monotonic() - ALARM_CAUSE_WINDOW
        recent = [
            (self.last_active[r.index], r.default_name)
            for r in inputs
            if self.last_active.get(r.index, 0.0) >= cutoff
        ]
        if recent:
            return max(recent)[1]
        for record in inputs:
            status = self.client.status.get(record.key)
            if isinstance(status, InputStatus) and status.active and not status.bypassed:
                return record.default_name
        return None

    def _track(self, update: StatusUpdate) -> list[tuple[RecordType, int]]:
        """Note input activity and alarms; return other records whose entities depend on this one."""
        status = update.status
        if isinstance(status, InputStatus):
            was = self._input_active.get(update.index, False)
            self._input_active[update.index] = status.active
            if status.active and not was:
                self.last_active[update.index] = time.monotonic()
            record = next((r for r in self.records_of(RecordType.INPUT) if r.index == update.index), None)
            return [(RecordType.AREA, area) for area in (record.areas or ())] if record else []
        if isinstance(status, AreaStatus):
            was = self._area_alarm.get(update.index, False)
            self._area_alarm[update.index] = status.alarm
            if status.alarm and not was and self.alarm_cause.get(update.index) is None:
                self.alarm_cause[update.index] = self._find_alarm_cause(update.index)
            elif not status.armed and not status.alarm:
                self.alarm_cause[update.index] = None
        return []

    # ----- lifecycle ------------------------------------------------------

    async def async_start(self) -> None:
        self._unsubs.append(self.client.add_status_listener(self._on_status))
        self._unsubs.append(self.client.add_connection_listener(self._on_connection))
        await self.client.start(self.records)

        if self.web:
            # The web client reads the event log itself.
            self._unsubs.append(self.client.add_listener(self._on_event))
            return
        port = self.entry.options.get(CONF_EVENTS_PORT)
        pin = self.entry.options.get(CONF_EVENTS_PIN)
        if port and pin:
            self.events = ProtegeEventClient(self.client.host, int(port), str(pin))
            self._unsubs.append(self.events.add_listener(self._on_event))
            await self.events.start()

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self.events:
            await self.events.stop()
        await self.client.stop()

    # ----- routing --------------------------------------------------------

    @callback
    def async_add_listener(self, key: tuple[RecordType, int], listener: CALLBACK_TYPE) -> CALLBACK_TYPE:
        self._listeners[key].append(listener)
        self._availability_listeners.append(listener)

        @callback
        def remove() -> None:
            self._listeners[key].remove(listener)
            self._availability_listeners.remove(listener)

        return remove

    @callback
    def async_add_availability_listener(self, listener: CALLBACK_TYPE) -> CALLBACK_TYPE:
        self._availability_listeners.append(listener)
        return lambda: self._availability_listeners.remove(listener)

    @callback
    def async_add_event_listener(self, listener: Callable[[PanelEvent], None]) -> CALLBACK_TYPE:
        self._event_listeners.append(listener)
        return lambda: self._event_listeners.remove(listener)

    @callback
    def async_set_arm_mode(self, area_index: int, mode: str, enabled: bool) -> None:
        disabled = self.disabled_arm_modes[area_index]
        if enabled:
            disabled.discard(mode)
        else:
            disabled.add(mode)
        for listener in list(self._listeners.get((RecordType.AREA, area_index), ())):
            listener()

    @callback
    def _on_status(self, update: StatusUpdate) -> None:
        dependants = self._track(update)
        for key in [(update.record_type, update.index), *dependants]:
            for listener in list(self._listeners.get(key, ())):
                listener()

    @callback
    def _on_connection(self, available: bool) -> None:
        for listener in list(self._availability_listeners):
            listener()

    @callback
    def _on_event(self, event: PanelEvent) -> None:
        self.hass.bus.async_fire(
            EVENT_PANEL,
            {
                "entry_id": self.entry.entry_id,
                "text": event.text,
                "category": event.category,
                "user": event.user,
                "code": event.code,
            },
        )
        if (kind := emergency_kind(event.text)) is not None:
            self._raise_emergency(kind, event)
        if "door" in (event.text or "").lower():
            # Changes made by other systems (keypads, Savant, cards) are not always pushed on
            # the control connection; the event is a cue to read the doors back.
            self.client.watch_doors()
        for listener in list(self._event_listeners):
            listener(event)

    @callback
    def _raise_emergency(self, kind: str, event: PanelEvent) -> None:
        """A duress, panic or medical alarm: an event, and a Repairs alert that stays until handled."""
        self.hass.bus.async_fire(
            EVENT_EMERGENCY,
            {"entry_id": self.entry.entry_id, "kind": kind, "text": event.text, "user": event.user},
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"emergency_{self.entry.entry_id}_{int(time.time())}",
            is_fixable=True,
            is_persistent=True,
            severity=ir.IssueSeverity.CRITICAL,
            translation_key="emergency",
            translation_placeholders={"kind": kind, "text": event.text or ""},
        )


def link_child_devices(hass: HomeAssistant, hub: ProtegeHub) -> None:
    """Point inputs and outputs at their module, and doors, areas and modules at the controller.

    Done through the registry rather than DeviceInfo.via_device, which newer
    Home Assistant releases deprecate.
    """
    dev_reg = dr.async_get(hass)
    devices = dr.async_entries_for_config_entry(dev_reg, hub.entry.entry_id)
    # Look the controller up within this entry: async_get_device is deprecated.
    controller = next((d for d in devices if (DOMAIN, hub.serial) in d.identifiers), None)
    if controller is None:
        return
    by_identifier = {ident: d for d in devices for ident in d.identifiers}
    parent_of: dict[str, str] = {}
    for record in hub.records_of(RecordType.INPUT) + hub.records_of(RecordType.OUTPUT):
        device = by_identifier.get((DOMAIN, hub.record_device_id(record)))
        module = by_identifier.get((DOMAIN, f"{hub.serial}_module_{record.module}"))
        if device is not None and module is not None:
            parent_of[device.id] = module.id
    for device in devices:
        if device.id == controller.id:
            continue
        parent = parent_of.get(device.id, controller.id)
        if device.via_device_id != parent:
            dev_reg.async_update_device(device.id, via_device_id=parent)


def remove_stale_devices(hass: HomeAssistant, hub: ProtegeHub) -> None:
    """Drop devices for records that no longer exist on the panel."""
    dev_reg = dr.async_get(hass)
    valid = {(DOMAIN, hub.serial)}
    for record in hub.records:
        if record.record_type is not RecordType.TROUBLE_INPUT:
            valid.add((DOMAIN, hub.record_device_id(record)))
    valid.update((DOMAIN, f"{hub.serial}_module_{module}") for module in hub.modules)
    for device in dr.async_entries_for_config_entry(dev_reg, hub.entry.entry_id):
        if not device.identifiers & valid:
            dev_reg.async_remove_device(device.id)


__all__ = ["CONF_PIN", "ProtegeHub", "records_from_data", "records_to_data", "remove_stale_devices"]
