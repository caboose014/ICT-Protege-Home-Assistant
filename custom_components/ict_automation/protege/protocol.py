"""Packet encoding and decoding for the ICT Automation and Control Service (ACS).

Reference: "ICT Automation and Control Service", ICT eSecurity, August 2014.
Only the binary, little-endian, unencrypted, 8-bit-sum checksum variant is supported.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass

HEADER_BINARY = b"IC"

PKT_TYPE_COMMAND = 0x00
PKT_TYPE_DATA = 0x01
PKT_TYPE_SYSTEM = 0xC0

FORMAT_NONE = 0x00

# Command groups
GROUP_SYSTEM = 0x00
GROUP_DOOR = 0x01
GROUP_AREA = 0x02
GROUP_OUTPUT = 0x03
GROUP_INPUT = 0x04
GROUP_VARIABLE = 0x05
GROUP_TROUBLE_INPUT = 0x06

# System sub commands
SYS_ARE_YOU_THERE = 0x00
SYS_PANEL_DESCRIPTION = 0x01
SYS_LOGIN = 0x02
SYS_LOGOUT = 0x03
SYS_SET_LOGIN_TIME = 0x04
SYS_MONITOR = 0x05
SYS_EVENTS = 0x06
SYS_ACK_CONFIG = 0x07

SUB_STATUS = 0x80

DOOR_LOCK = 0x00
DOOR_UNLOCK_MOMENTARY = 0x01
DOOR_UNLOCK_LATCHED = 0x02

AREA_DISARM = 0x00
AREA_DISARM_24H = 0x01
AREA_DISARM_ALL = 0x02
AREA_ARM_NORMAL = 0x03
AREA_ARM_FORCE = 0x04
AREA_ARM_STAY = 0x05
AREA_ARM_INSTANT = 0x06

OUTPUT_OFF = 0x00
OUTPUT_ON = 0x01
OUTPUT_ON_TIMED = 0x02

INPUT_BYPASS_REMOVE = 0x00
INPUT_BYPASS_TEMPORARY = 0x01
INPUT_BYPASS_PERMANENT = 0x02

SYSTEM_ACK = b"\xff\x00"
SYSTEM_NACK = b"\xff\xff"
DATA_TERMINATOR = (0xFF, 0xFF)

# Data block types, as (type_low, type_high). For record status the high byte is the group.
DATA_PANEL_SERIAL = (0x00, 0x00)
DATA_PANEL_HW_VERSION = (0x01, 0x00)
DATA_FIRMWARE_TYPE = (0x02, 0x00)
DATA_FIRMWARE_VERSION = (0x03, 0x00)
DATA_FIRMWARE_BUILD = (0x04, 0x00)
DATA_EVENT_NUMERIC = (0x00, 0x30)
DATA_EVENT_TEXT = (0x01, 0x30)

# Nack error codes
NACK_SERVICE_COMMAND_NOT_VALID = 0x0120
NACK_SERVICE_INDEX_NOT_VALID = 0x0121
NACK_USER_LOGIN = 0x0300
NACK_USER_LOGOUT = 0x0301
NACK_USER_INVALID = 0x0302
NACK_USER_AXS_AREA = 0x0303
NACK_USER_DOOR_GROUP = 0x030A
NACK_USER_AXS_DOOR_AXS_LVL = 0x030F
NACK_INPUT_COMMAND_FAILED = 0x040E
NACK_AREA_NO_CHANGE = 0x0869
NACK_DOOR_DENIED_LOCKDOWN = 0x0A23
NACK_DOOR_INTERLOCK_ACTIVE = 0x0A12
NACK_DOOR_ALREADY_IN_STATE = 0x0A32

NACK_MESSAGES = {
    NACK_SERVICE_COMMAND_NOT_VALID: "The panel does not accept this command",
    NACK_SERVICE_INDEX_NOT_VALID: "The record does not exist on the panel",
    NACK_USER_LOGIN: "A user is already logged in",
    NACK_USER_LOGOUT: "No user is logged in",
    NACK_USER_INVALID: "The PIN is not valid",
    NACK_USER_AXS_AREA: "The user does not have rights for this area",
    NACK_USER_DOOR_GROUP: "The user does not have rights for this door",
    NACK_USER_AXS_DOOR_AXS_LVL: "The user has no access level for this door",
    NACK_INPUT_COMMAND_FAILED: "The input cannot be bypassed (check its configuration or whether its area is armed)",
    NACK_AREA_NO_CHANGE: "The area is already in that state",
    NACK_DOOR_DENIED_LOCKDOWN: "The door is in lockdown",
    NACK_DOOR_INTERLOCK_ACTIVE: "The door is interlocked",
    NACK_DOOR_ALREADY_IN_STATE: "The door is already in that state",
}

# Nacks that mean "nothing to do" rather than failure.
NACK_HARMLESS = {NACK_AREA_NO_CHANGE, NACK_DOOR_ALREADY_IN_STATE}


class ICTProtocolError(ValueError):
    """Raised when an ICT packet cannot be decoded."""


@dataclass(frozen=True)
class ICTPacket:
    packet_type: int
    format_flags: int
    data: bytes
    checksum: int | None = None

    @property
    def is_ack(self) -> bool:
        return self.packet_type == PKT_TYPE_SYSTEM and self.data[:2] == SYSTEM_ACK

    @property
    def is_nack(self) -> bool:
        return self.packet_type == PKT_TYPE_SYSTEM and self.data[:2] == SYSTEM_NACK

    @property
    def nack_code(self) -> int | None:
        """Return the Nack error code, if the panel supplied one."""
        if not self.is_nack or len(self.data) < 4:
            return None
        return struct.unpack("<H", self.data[2:4])[0]


@dataclass(frozen=True)
class ICTDataBlock:
    type_low: int
    type_high: int
    body: bytes

    @property
    def data_type(self) -> tuple[int, int]:
        return (self.type_low, self.type_high)


def checksum_8bit(packet_without_checksum: bytes) -> int:
    """Return the ICT 8-bit sum checksum."""
    return sum(packet_without_checksum) % 256


def build_packet(
    packet_type: int,
    data: bytes = b"",
    *,
    format_flags: int = FORMAT_NONE,
    checksum: bool = True,
) -> bytes:
    """Build a binary ICT packet."""
    length = 6 + len(data) + (1 if checksum else 0)
    packet = bytearray(HEADER_BINARY)
    packet.extend(struct.pack("<H", length))
    packet.append(packet_type)
    packet.append(format_flags)
    packet.extend(data)
    if checksum:
        packet.append(checksum_8bit(packet))
    return bytes(packet)


def build_command_packet(
    group: int,
    sub_command: int,
    data: bytes = b"",
    *,
    checksum: bool = True,
) -> bytes:
    """Build a command packet from command group, sub command, and payload."""
    return build_packet(
        PKT_TYPE_COMMAND,
        bytes((group, sub_command)) + data,
        checksum=checksum,
    )


def build_record_command(group: int, sub_command: int, index: int, extra: bytes = b"") -> bytes:
    """Build a control or status command addressed to one record index."""
    return build_command_packet(group, sub_command, struct.pack("<I", index) + extra)


def build_login(pin: str) -> bytes:
    """Build a login packet. The PIN is up to six digits, terminated by 0xFF if shorter."""
    digits = [int(c) for c in str(pin) if c.isdigit()]
    if not digits or len(digits) > 6:
        raise ValueError("PIN must be 1 to 6 digits")
    payload = bytearray(digits)
    if len(digits) < 6:
        payload.append(0xFF)
    return build_command_packet(GROUP_SYSTEM, SYS_LOGIN, bytes(payload))


def build_monitor(group: int, index: int, *, start: bool = True, force_update: bool = True) -> bytes:
    """Build a Request to Monitor packet for one record."""
    flags = (0x01 if start else 0x00) | (0x02 if force_update else 0x00)
    payload = bytes((group, 0x00)) + struct.pack("<I", index) + bytes((flags, 0x00))
    return build_command_packet(GROUP_SYSTEM, SYS_MONITOR, payload)


def build_request_events(*, start: bool = True, human_readable: bool = True) -> bytes:
    """Build a Request Events packet."""
    return build_command_packet(
        GROUP_SYSTEM,
        SYS_EVENTS,
        bytes((0x01 if start else 0x00, 0x01 if human_readable else 0x00)),
    )


def build_set_login_time(seconds: int) -> bytes:
    """Build a Set Login Time packet (the panel caps it at 100 minutes)."""
    return build_command_packet(GROUP_SYSTEM, SYS_SET_LOGIN_TIME, struct.pack("<H", min(seconds, 6000)))


def build_ack() -> bytes:
    """Build the Ack a client sends for unsolicited data packets."""
    return build_packet(PKT_TYPE_SYSTEM, SYSTEM_ACK)


def parse_packet(raw: bytes, *, checksum: bool = True) -> ICTPacket:
    """Parse a complete binary ICT packet."""
    if len(raw) < 6:
        raise ICTProtocolError("Packet is too short")
    if raw[:2] != HEADER_BINARY:
        raise ICTProtocolError("Packet header is not IC")

    length = struct.unpack("<H", raw[2:4])[0]
    if length != len(raw):
        raise ICTProtocolError(f"Packet length {length} does not match {len(raw)} bytes")

    expected_checksum = None
    data_end = length
    if checksum:
        expected_checksum = checksum_8bit(raw[:-1])
        actual_checksum = raw[-1]
        if actual_checksum != expected_checksum:
            raise ICTProtocolError(f"Checksum {actual_checksum:#04x} does not match {expected_checksum:#04x}")
        data_end -= 1

    return ICTPacket(
        packet_type=raw[4],
        format_flags=raw[5],
        data=raw[6:data_end],
        checksum=raw[-1] if checksum else None,
    )


def iter_data_blocks(data: bytes) -> Iterator[ICTDataBlock]:
    """Yield data blocks from an ICT data packet payload."""
    offset = 0
    while offset + 3 <= len(data):
        type_low = data[offset]
        type_high = data[offset + 1]
        length = data[offset + 2]
        offset += 3
        if (type_low, type_high) == DATA_TERMINATOR:
            break
        body = data[offset : offset + length]
        if len(body) != length:
            raise ICTProtocolError("Data block ended before declared length")
        yield ICTDataBlock(type_low, type_high, body)
        offset += length


def build_data_packet(blocks: list[tuple[tuple[int, int], bytes]]) -> bytes:
    """Build a data packet (used by tests and the simulator)."""
    payload = bytearray()
    for (type_low, type_high), body in blocks:
        payload.extend((type_low, type_high, len(body)))
        payload.extend(body)
    payload.extend((0xFF, 0xFF, 0x00))
    return build_packet(PKT_TYPE_DATA, bytes(payload))


class PacketReader:
    """Reassemble packets from a TCP byte stream."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buffer.extend(chunk)
        packets: list[bytes] = []
        while len(self._buffer) >= 4:
            if self._buffer[:2] != HEADER_BINARY:
                # Resynchronise on the next header.
                next_header = self._buffer.find(HEADER_BINARY, 1)
                if next_header == -1:
                    del self._buffer[:-1]
                    break
                del self._buffer[:next_header]
                continue
            length = struct.unpack("<H", self._buffer[2:4])[0]
            if length < 6:
                del self._buffer[:2]
                continue
            if len(self._buffer) < length:
                break
            packets.append(bytes(self._buffer[:length]))
            del self._buffer[:length]
        return packets
