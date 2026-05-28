"""Packet helpers for the ICT Automation and Control Service."""

from __future__ import annotations

from dataclasses import dataclass
import struct

HEADER_BINARY = b"IC"

PKT_TYPE_COMMAND = 0x00
PKT_TYPE_DATA = 0x01
PKT_TYPE_SYSTEM = 0xC0

FORMAT_NONE = 0x00

GROUP_SYSTEM = 0x00
GROUP_DOOR = 0x01
GROUP_AREA = 0x02
GROUP_OUTPUT = 0x03
GROUP_INPUT = 0x04
GROUP_VARIABLE = 0x05
GROUP_TROUBLE_INPUT = 0x06

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

SYSTEM_ACK = b"\xFF\x00"
SYSTEM_NACK = b"\xFF\xFF"
DATA_TERMINATOR = (0xFF, 0xFF)


class ICTProtocolError(ValueError):
    """Raised when an ICT packet cannot be decoded."""


@dataclass(frozen=True)
class ICTPacket:
    packet_type: int
    format_flags: int
    data: bytes
    checksum: int | None = None


@dataclass(frozen=True)
class ICTDataBlock:
    type_low: int
    type_high: int
    body: bytes


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
            raise ICTProtocolError(
                f"Checksum {actual_checksum:#04x} does not match {expected_checksum:#04x}"
            )
        data_end -= 1

    return ICTPacket(
        packet_type=raw[4],
        format_flags=raw[5],
        data=raw[6:data_end],
        checksum=raw[-1] if checksum else None,
    )


def iter_data_blocks(data: bytes):
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
