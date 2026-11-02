"""A small simulated ICT ACS service, built from the protocol PDF, for tests."""

from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass, field

from custom_components.ict_automation.protege import protocol as p


@dataclass
class _Session:
    writer: asyncio.StreamWriter
    user: str | None = None
    monitored: set[tuple[int, int]] = field(default_factory=set)
    events: bool = False


@dataclass
class FakeUser:
    doors: bool = True
    outputs: bool = True
    bypass: bool = True
    arm: bool = False


class FakePanel:
    def __init__(self, *, allow_status_logged_out: bool = True) -> None:
        self.allow_status_logged_out = allow_status_logged_out
        self.users: dict[str, FakeUser] = {
            "1234": FakeUser(),  # saved integration PIN: doors and outputs, no arming
            "5678": FakeUser(doors=False, outputs=False, bypass=False, arm=True),  # a person
            "4321": FakeUser(doors=False, outputs=False, bypass=False),  # view-only events user
        }
        self.doors = {1: [0, 0], 2: [0, 0]}
        self.areas = {1: [0x00, 0x00, 0x00]}
        self.outputs = {1: ["CP001:01", 0], 2: ["CP001:02", 0]}
        self.inputs = {1: ["CP001:01", 0, 0], 2: ["CP001:02", 0, 0], 3: ["ZX001:01", 0, 0]}
        self.troubles = {1: ["CP001:01", 0, 0]}
        self.serial = 0x00A1B2C3
        self.commands: list[tuple[int, int, int]] = []
        self.sessions: list[_Session] = []
        self._server: asyncio.base_events.Server | None = None
        self.port = 0

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        for s in list(self.sessions):
            s.writer.close()
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    # ----- helpers --------------------------------------------------------

    def _table(self, group: int) -> dict:
        return {
            p.GROUP_DOOR: self.doors,
            p.GROUP_AREA: self.areas,
            p.GROUP_OUTPUT: self.outputs,
            p.GROUP_INPUT: self.inputs,
            p.GROUP_TROUBLE_INPUT: self.troubles,
        }[group]

    def status_block(self, group: int, index: int) -> tuple[tuple[int, int], bytes]:
        idx = struct.pack("<I", index)
        rec = self._table(group)[index]
        if group == p.GROUP_DOOR:
            body = idx + bytes((rec[0], rec[1], 0, 0))
        elif group == p.GROUP_AREA:
            body = idx + bytes((rec[0], rec[1], rec[2], 0))
        elif group == p.GROUP_OUTPUT:
            body = idx + rec[0].encode() + bytes((rec[1], 0, 0, 0))
        else:
            body = idx + rec[0].encode() + bytes((rec[1], rec[2], 0, 0))
        return ((0x00, group), body)

    @staticmethod
    def _ack() -> bytes:
        return p.build_packet(p.PKT_TYPE_SYSTEM, p.SYSTEM_ACK)

    @staticmethod
    def _nack(code: int) -> bytes:
        return p.build_packet(p.PKT_TYPE_SYSTEM, p.SYSTEM_NACK + struct.pack("<H", code) + bytes(6))

    def push(self, group: int, index: int) -> None:
        """Send a status change to every session monitoring the record."""
        raw = p.build_data_packet([self.status_block(group, index)])
        for s in self.sessions:
            if (group, index) in s.monitored:
                s.writer.write(raw)

    def push_event(self, text: str) -> None:
        raw = p.build_data_packet([(p.DATA_EVENT_TEXT, text.encode() + b"\x00")])
        for s in self.sessions:
            if s.events and s.user:
                s.writer.write(raw)

    def set_input(self, index: int, state: int) -> None:
        self.inputs[index][1] = state
        self.push(p.GROUP_INPUT, index)

    # ----- server ---------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        session = _Session(writer)
        self.sessions.append(session)
        assembler = p.PacketReader()
        try:
            while chunk := await reader.read(4096):
                for raw in assembler.feed(chunk):
                    packet = p.parse_packet(raw)
                    if packet.packet_type == p.PKT_TYPE_COMMAND:
                        for out in self._process(session, packet.data):
                            writer.write(out)
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self.sessions.remove(session)
            writer.close()

    def _process(self, s: _Session, data: bytes) -> list[bytes]:
        group, sub = data[0], data[1]
        payload = data[2:]
        if group == p.GROUP_SYSTEM:
            return self._system(s, sub, payload)
        index = struct.unpack("<I", payload[:4])[0]
        table = self._table(group)
        if sub == p.SUB_STATUS:
            if s.user is None and not self.allow_status_logged_out:
                return [self._nack(p.NACK_USER_LOGOUT)]
            if index not in table:
                return [self._nack(p.NACK_SERVICE_INDEX_NOT_VALID)]
            return [p.build_data_packet([self.status_block(group, index)])]
        if s.user is None:
            return [self._nack(p.NACK_USER_LOGOUT)]
        if index not in table:
            return [self._nack(p.NACK_SERVICE_INDEX_NOT_VALID)]
        user = self.users[s.user]
        self.commands.append((group, sub, index))
        if group == p.GROUP_DOOR:
            if not user.doors:
                return [self._nack(p.NACK_USER_AXS_DOOR_AXS_LVL)]
            table[index][0] = 0 if sub == p.DOOR_LOCK else (4 if sub == p.DOOR_UNLOCK_LATCHED else 1)
        elif group == p.GROUP_AREA:
            if not user.arm:
                return [self._nack(p.NACK_USER_AXS_AREA)]
            if sub in (p.AREA_DISARM, p.AREA_DISARM_ALL, p.AREA_DISARM_24H):
                table[index][0], table[index][2] = 0x00, 0x00
            else:
                flags = {p.AREA_ARM_STAY: 0x40, p.AREA_ARM_INSTANT: 0x20, p.AREA_ARM_FORCE: 0x10}.get(sub, 0)
                table[index][0], table[index][2] = 0x80, flags
        elif group == p.GROUP_OUTPUT:
            if not user.outputs:
                return [self._nack(p.NACK_USER_AXS_AREA)]
            table[index][1] = 0 if sub == p.OUTPUT_OFF else (3 if sub == p.OUTPUT_ON_TIMED else 1)
        elif group in (p.GROUP_INPUT, p.GROUP_TROUBLE_INPUT):
            if not user.bypass:
                return [self._nack(p.NACK_INPUT_COMMAND_FAILED)]
            table[index][2] = {
                p.INPUT_BYPASS_REMOVE: 0,
                p.INPUT_BYPASS_TEMPORARY: 1,
                p.INPUT_BYPASS_PERMANENT: 3,
            }[sub]
        self.push(group, index)
        return [self._ack()]

    def _system(self, s: _Session, sub: int, payload: bytes) -> list[bytes]:
        if sub == p.SYS_ARE_YOU_THERE:
            return [self._ack()]
        if sub == p.SYS_PANEL_DESCRIPTION:
            return [
                p.build_data_packet(
                    [
                        (p.DATA_PANEL_SERIAL, struct.pack("<I", self.serial)),
                        (p.DATA_PANEL_HW_VERSION, b"\x01"),
                        (p.DATA_FIRMWARE_TYPE, b"WX"),
                        (p.DATA_FIRMWARE_VERSION, struct.pack("<H", 400)),
                        (p.DATA_FIRMWARE_BUILD, struct.pack("<H", 1676)),
                    ]
                )
            ]
        if sub == p.SYS_LOGIN:
            if s.user is not None:
                return [self._nack(p.NACK_USER_LOGIN)]
            digits = []
            for b in payload:
                if b > 9:
                    break
                digits.append(str(b))
            pin = "".join(digits)
            if pin not in self.users:
                return [self._nack(p.NACK_USER_INVALID)]
            s.user = pin
            s.monitored.clear()
            s.events = False
            return [self._ack()]
        if sub == p.SYS_LOGOUT:
            s.user = None
            s.monitored.clear()
            s.events = False
            return [self._ack()]
        if sub == p.SYS_SET_LOGIN_TIME:
            return [self._ack()]
        if sub == p.SYS_MONITOR:
            group = payload[0]
            index = struct.unpack("<I", payload[2:6])[0]
            flags = payload[6]
            if s.user is None and not self.allow_status_logged_out:
                return [self._nack(p.NACK_USER_LOGOUT)]
            if index not in self._table(group):
                return [self._nack(p.NACK_SERVICE_INDEX_NOT_VALID)]
            out = [self._ack()]
            if flags & 0x01:
                s.monitored.add((group, index))
                if flags & 0x02:
                    out.append(p.build_data_packet([self.status_block(group, index)]))
            else:
                s.monitored.discard((group, index))
            return out
        if sub == p.SYS_EVENTS:
            if s.user is None:
                return [self._nack(p.NACK_USER_LOGOUT)]
            s.events = bool(payload[0] & 0x01)
            return [self._ack()]
        return [self._nack(p.NACK_SERVICE_COMMAND_NOT_VALID)]
