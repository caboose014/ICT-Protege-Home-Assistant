"""Hub that owns the panel connections and routes updates to entities."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo

from .const import (
    CONF_EVENTS_PIN,
    CONF_EVENTS_PORT,
    CONF_PIN,
    CONF_RECORDS,
    DOMAIN,
    EVENT_PANEL,
    MANUFACTURER,
)
from .protege import (
    PanelEvent,
    PanelInfo,
    ProtegeClient,
    ProtegeEventClient,
    Record,
    RecordType,
    StatusUpdate,
)

_LOGGER = logging.getLogger(__name__)

CONTROLLER_MODULE = "CP001"


def records_to_data(records: list[Record]) -> list[dict[str, Any]]:
    return [{"type": r.record_type.value, "index": r.index, "reference": r.reference} for r in records]


def records_from_data(data: list[dict[str, Any]]) -> list[Record]:
    return [Record(RecordType(d["type"]), int(d["index"]), d.get("reference")) for d in data]


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

    # ----- identity -------------------------------------------------------

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
            configuration_url=f"https://{self.client.host}",
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
        """Doors and areas each get their own device."""
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.serial}_{record.record_type.value}_{record.index}")},
            name=record.default_name,
            manufacturer=MANUFACTURER,
            model=f"Protege {record.record_type.value}",
        )

    def records_of(self, record_type: RecordType) -> list[Record]:
        return [r for r in self.records if r.record_type is record_type]

    # ----- lifecycle ------------------------------------------------------

    async def async_start(self) -> None:
        self._unsubs.append(self.client.add_status_listener(self._on_status))
        self._unsubs.append(self.client.add_connection_listener(self._on_connection))
        await self.client.start(self.records)

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
    def _on_status(self, update: StatusUpdate) -> None:
        for listener in list(self._listeners.get((update.record_type, update.index), ())):
            listener()

    @callback
    def _on_connection(self, available: bool) -> None:
        for listener in list(self._availability_listeners):
            listener()

    @callback
    def _on_event(self, event: PanelEvent) -> None:
        self.hass.bus.async_fire(
            EVENT_PANEL,
            {"entry_id": self.entry.entry_id, "text": event.text, "code": event.code},
        )
        for listener in list(self._event_listeners):
            listener(event)


def link_child_devices(hass: HomeAssistant, hub: ProtegeHub) -> None:
    """Point door, area and module devices at the controller.

    Done through the registry rather than DeviceInfo.via_device, which newer
    Home Assistant releases deprecate.
    """
    dev_reg = dr.async_get(hass)
    controller = dev_reg.async_get_device(identifiers={(DOMAIN, hub.serial)})
    if controller is None:
        return
    for device in dr.async_entries_for_config_entry(dev_reg, hub.entry.entry_id):
        if device.id != controller.id and device.via_device_id != controller.id:
            dev_reg.async_update_device(device.id, via_device_id=controller.id)


def remove_stale_devices(hass: HomeAssistant, hub: ProtegeHub) -> None:
    """Drop devices for records that no longer exist on the panel."""
    dev_reg = dr.async_get(hass)
    valid = {(DOMAIN, hub.serial)}
    for record in hub.records:
        if record.record_type in (RecordType.DOOR, RecordType.AREA):
            valid.add((DOMAIN, f"{hub.serial}_{record.record_type.value}_{record.index}"))
        elif record.module and record.module != CONTROLLER_MODULE:
            valid.add((DOMAIN, f"{hub.serial}_module_{record.module}"))
    for device in dr.async_entries_for_config_entry(dev_reg, hub.entry.entry_id):
        if not device.identifiers & valid:
            dev_reg.async_update_device(device.id, remove_config_entry_id=hub.entry.entry_id)


__all__ = ["CONF_PIN", "ProtegeHub", "records_from_data", "records_to_data", "remove_stale_devices"]
