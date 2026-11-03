"""Typed records and status decoding for Protege panels."""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum

from . import protocol as p


class RecordType(StrEnum):
    """Kinds of panel records exposed over ACS."""

    DOOR = "door"
    AREA = "area"
    OUTPUT = "output"
    INPUT = "input"
    TROUBLE_INPUT = "trouble_input"

    @property
    def group(self) -> int:
        return _GROUPS[self]

    @classmethod
    def from_group(cls, group: int) -> RecordType | None:
        return _BY_GROUP.get(group)


_GROUPS = {
    RecordType.DOOR: p.GROUP_DOOR,
    RecordType.AREA: p.GROUP_AREA,
    RecordType.OUTPUT: p.GROUP_OUTPUT,
    RecordType.INPUT: p.GROUP_INPUT,
    RecordType.TROUBLE_INPUT: p.GROUP_TROUBLE_INPUT,
}
_BY_GROUP = {v: k for k, v in _GROUPS.items()}


class DoorLockState(IntEnum):
    LOCKED = 0x00
    UNLOCKED_USER = 0x01
    UNLOCKED_SCHEDULE = 0x02
    UNLOCKED_USER_TIMED = 0x03
    UNLOCKED_LATCHED = 0x04
    UNLOCKED_REX = 0x05
    UNLOCKED_REN = 0x06
    UNLOCKED_KEYPAD = 0x07
    UNLOCKED_AREA = 0x08
    UNLOCKED_FIRE = 0x09


class DoorState(IntEnum):
    CLOSED = 0x00
    OPEN = 0x01
    OPEN_ALERT = 0x02
    LEFT_OPEN = 0x03
    FORCED_OPEN = 0x04


class AreaState(IntEnum):
    DISARMED = 0x00
    INPUTS_OPEN = 0x01
    TROUBLE = 0x02
    BYPASS_ERROR = 0x03
    BYPASS_WARNING = 0x04
    USER_COUNT_NOT_ZERO = 0x05
    ARMED = 0x80
    EXIT_DELAY = 0x81
    ENTRY_DELAY = 0x82
    DISARM_DELAY = 0x83
    CODE_DELAY = 0x84


class InputState(IntEnum):
    CLOSED = 0x00
    OPEN = 0x01
    SHORT = 0x02
    TAMPER = 0x03


class OutputState(IntEnum):
    OFF = 0x00
    ON = 0x01
    ON_PULSED = 0x02
    ON_TIMED = 0x03
    ON_PULSED_TIMED = 0x04


def _enum_or_int(enum_cls, value: int):
    try:
        return enum_cls(value)
    except ValueError:
        return value


@dataclass
class DoorStatus:
    lock_state: DoorLockState | int
    door_state: DoorState | int

    @property
    def locked(self) -> bool:
        return self.lock_state == DoorLockState.LOCKED

    @property
    def open(self) -> bool:
        return self.door_state != DoorState.CLOSED


@dataclass
class AreaStatus:
    state: AreaState | int
    tamper_state: int
    flags: int

    @property
    def armed(self) -> bool:
        return int(self.state) >= 0x80

    @property
    def alarm(self) -> bool:
        return bool(self.flags & 0x01)

    @property
    def siren(self) -> bool:
        return bool(self.flags & 0x02)

    @property
    def alarm_memory(self) -> bool:
        return bool(self.flags & 0x04)

    @property
    def remote_armed(self) -> bool:
        return bool(self.flags & 0x08)

    @property
    def force_armed(self) -> bool:
        return bool(self.flags & 0x10)

    @property
    def instant_armed(self) -> bool:
        return bool(self.flags & 0x20)

    @property
    def partial_armed(self) -> bool:
        return bool(self.flags & 0x40)


@dataclass
class OutputStatus:
    reference: str
    state: OutputState | int

    @property
    def on(self) -> bool:
        return self.state != OutputState.OFF


@dataclass
class InputStatus:
    reference: str
    state: InputState | int
    bypass_flags: int

    @property
    def active(self) -> bool:
        return self.state != InputState.CLOSED

    @property
    def bypassed(self) -> bool:
        return bool(self.bypass_flags & 0x01)

    @property
    def bypass_latched(self) -> bool:
        return bool(self.bypass_flags & 0x02)

    @property
    def siren_lockout(self) -> bool:
        return bool(self.bypass_flags & 0x08)


Status = DoorStatus | AreaStatus | OutputStatus | InputStatus


@dataclass(frozen=True)
class StatusUpdate:
    record_type: RecordType
    index: int
    status: Status


@dataclass
class Record:
    """A record discovered on the panel."""

    record_type: RecordType
    index: int
    reference: str | None = None
    name: str | None = None

    @property
    def key(self) -> tuple[RecordType, int]:
        return (self.record_type, self.index)

    @property
    def module(self) -> str | None:
        """Module the record is wired to, e.g. 'CP001' or 'ZX002'."""
        if self.reference and ":" in self.reference:
            return self.reference.split(":", 1)[0]
        return None

    @property
    def default_name(self) -> str:
        if self.name:
            return self.name
        label = {
            RecordType.DOOR: "Door",
            RecordType.AREA: "Area",
            RecordType.OUTPUT: "Output",
            RecordType.INPUT: "Input",
            RecordType.TROUBLE_INPUT: "Trouble input",
        }[self.record_type]
        if self.reference:
            return f"{label} {self.reference}"
        return f"{label} {self.index}"


@dataclass
class PanelInfo:
    serial: int | None = None
    hardware_version: int | None = None
    firmware_type: str | None = None
    firmware_version: int | None = None
    firmware_build: int | None = None

    @property
    def serial_str(self) -> str | None:
        return None if self.serial is None else f"{self.serial:08X}"

    @property
    def firmware(self) -> str | None:
        if self.firmware_version is None:
            return None
        # Assumed encoding: 400 -> "4.00" (matches ICT version strings like 4.00.1676).
        major, minor = divmod(self.firmware_version, 100)
        text = f"{major}.{minor:02d}"
        if self.firmware_build is not None:
            text += f".{self.firmware_build}"
        if self.firmware_type:
            text = f"{self.firmware_type} {text}"
        return text


@dataclass
class PanelEvent:
    text: str | None = None
    user: str | None = None
    category: str | None = None  # first field of a human readable event, e.g. "Door", "Input"
    description: str | None = None
    code: int | None = None
    data: bytes = field(default=b"")


def _reference(body: bytes) -> str:
    return body[4:12].decode("ascii", errors="replace").strip("\x00 ")


def decode_status(block: p.ICTDataBlock) -> StatusUpdate | None:
    """Decode a record status data block, or return None for other blocks."""
    record_type = RecordType.from_group(block.type_high)
    if record_type is None or block.type_low != 0x00 or len(block.body) < 6:
        return None
    body = block.body
    index = struct.unpack("<I", body[0:4])[0]
    status: Status
    if record_type is RecordType.DOOR:
        status = DoorStatus(_enum_or_int(DoorLockState, body[4]), _enum_or_int(DoorState, body[5]))
    elif record_type is RecordType.AREA:
        if len(body) < 7:
            return None
        status = AreaStatus(_enum_or_int(AreaState, body[4]), body[5], body[6])
    elif record_type is RecordType.OUTPUT:
        if len(body) < 13:
            return None
        status = OutputStatus(_reference(body), _enum_or_int(OutputState, body[12]))
    else:
        if len(body) < 14:
            return None
        status = InputStatus(_reference(body), _enum_or_int(InputState, body[12]), body[13])
    return StatusUpdate(record_type, index, status)


def decode_panel_info(blocks: list[p.ICTDataBlock], info: PanelInfo | None = None) -> PanelInfo:
    info = info or PanelInfo()
    for block in blocks:
        body = block.body
        if block.data_type == p.DATA_PANEL_SERIAL and len(body) >= 4:
            info.serial = struct.unpack("<I", body[:4])[0]
        elif block.data_type == p.DATA_PANEL_HW_VERSION and body:
            info.hardware_version = body[0]
        elif block.data_type == p.DATA_FIRMWARE_TYPE and len(body) >= 2:
            info.firmware_type = body[:2].decode("ascii", errors="replace")
        elif block.data_type == p.DATA_FIRMWARE_VERSION and len(body) >= 2:
            info.firmware_version = struct.unpack("<H", body[:2])[0]
        elif block.data_type == p.DATA_FIRMWARE_BUILD and len(body) >= 2:
            info.firmware_build = struct.unpack("<H", body[:2])[0]
    return info


# Words that follow the user's name in "User" events, e.g. "Savant Unlocked Door Jail Gate At Savant".
_USER_ACTIONS = (
    " Remote Login",
    " Logged In",
    " Logged Out",
    " Entry Granted",
    " Exit Granted",
    " Access Granted",
    " Access Denied",
    " Denied",
    " Granted",
    " Unlocked",
    " Locked",
    " Disarmed",
    " Armed",
)


def event_user(event: PanelEvent) -> str | None:
    """The user a "User" event names, if it can be found."""
    if (event.category or "").lower() != "user" or not event.description:
        return None
    text = event.description
    cut = min((i for a in _USER_ACTIONS if (i := text.find(a)) > 0), default=-1)
    return text[:cut].strip() or None if cut > 0 else None


# Card numbers in event text, e.g. "... Using User By Card [Card 321:5]". They are
# left out so they don't end up in HA's history, where they could help clone a card.
_CARD_NUMBER = re.compile(r"\s*\[Card [^\]]*\]")


def decode_event(block: p.ICTDataBlock) -> PanelEvent | None:
    # The spec's tables give these as high byte 0x30, but its packet layouts put 0x30 first,
    # so accept either byte order.
    if block.data_type in (p.DATA_EVENT_TEXT, p.DATA_EVENT_TEXT[::-1]):
        # Some panels separate the fields with NULs; join them rather than keep only the first.
        # On site: b"Door\x00Front Door Unlocked By REX\x00".
        body = block.body.decode("ascii", errors="replace")
        raw = [_CARD_NUMBER.sub("", part).strip() for part in body.split("\x00")]
        parts = [part for part in raw if part]
        if not parts:
            return None
        # A leading empty field means the event has no category.
        category = parts[0] if len(parts) > 1 and raw[0] else None
        description = " ".join(parts[1:]) if category else " ".join(parts)
        event = PanelEvent(text=" ".join(parts), category=category, description=description)
        event.user = event_user(event)
        if not raw[0] and event.user is None:
            # Events with an empty category also name a user, e.g. on site:
            # b"\x00Jordan McDonald Unlocked Door Jail Gate \x00".
            event.user = event_user(PanelEvent(category="user", description=event.description))
        return event
    if block.data_type in (p.DATA_EVENT_NUMERIC, p.DATA_EVENT_NUMERIC[::-1]) and len(block.body) >= 2:
        return PanelEvent(code=struct.unpack("<H", block.body[:2])[0], data=bytes(block.body[2:8]))
    return None
