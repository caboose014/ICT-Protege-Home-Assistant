"""Client tests against the simulated panel."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.ict_automation.protege import (
    CommandRejected,
    InvalidPin,
    ProtegeClient,
    ProtegeEventClient,
    RecordType,
)
from custom_components.ict_automation.protege import protocol as p


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    async def loop():
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(loop(), timeout)


async def test_connect_reads_panel_description(panel):
    client = ProtegeClient("127.0.0.1", panel.port, "1234")
    info = await client.connect()
    assert info.serial_str == "00A1B2C3"
    assert info.firmware == "WX 4.00.1676"
    await client.disconnect()


async def test_discovery_finds_all_records(panel):
    client = ProtegeClient("127.0.0.1", panel.port, "1234")
    await client.connect()
    records = await client.discover()
    keys = {r.key for r in records}
    assert keys == {
        (RecordType.AREA, 1),
        (RecordType.DOOR, 1),
        (RecordType.DOOR, 2),
        (RecordType.OUTPUT, 1),
        (RecordType.OUTPUT, 2),
        (RecordType.INPUT, 1),
        (RecordType.INPUT, 2),
        (RecordType.INPUT, 3),
        (RecordType.TROUBLE_INPUT, 1),
    }
    zx = next(r for r in records if r.key == (RecordType.INPUT, 3))
    assert zx.reference == "ZX001:01"
    assert zx.module == "ZX001"
    await client.disconnect()


async def test_discovery_logs_in_when_status_needs_login(panel):
    panel.allow_status_logged_out = False
    client = ProtegeClient("127.0.0.1", panel.port, "1234")
    await client.connect()
    records = await client.discover()
    assert len(records) == 9
    assert all(s.user is None for s in panel.sessions)
    await client.disconnect()


async def test_monitoring_survives_commands(panel):
    client = ProtegeClient("127.0.0.1", panel.port, "1234")
    await client.connect()
    records = await client.discover()
    updates = []
    client.add_status_listener(updates.append)
    await client.start(records)
    assert client.available

    await client.door_unlock_latched(1)
    assert (p.GROUP_DOOR, p.DOOR_UNLOCK_LATCHED, 1) in panel.commands
    await _wait_until(lambda: not client.status[(RecordType.DOOR, 1)].locked)
    # After the command the client logged out and re-registered monitoring.
    session = panel.sessions[0]
    assert session.user is None
    assert (p.GROUP_INPUT, 3) in session.monitored

    updates.clear()
    panel.set_input(3, 1)
    await _wait_until(lambda: any(u.record_type is RecordType.INPUT and u.index == 3 for u in updates))
    assert client.status[(RecordType.INPUT, 3)].active
    await client.stop()


async def test_arming_needs_a_user_with_rights(panel):
    client = ProtegeClient("127.0.0.1", panel.port, "1234")
    await client.connect()
    await client.start(await client.discover())
    with pytest.raises(CommandRejected) as err:
        await client.area_command(1, p.AREA_ARM_NORMAL, "1234")
    assert err.value.code == p.NACK_USER_AXS_AREA
    with pytest.raises(InvalidPin):
        await client.area_command(1, p.AREA_ARM_NORMAL, "0000")
    await client.area_command(1, p.AREA_ARM_STAY, "5678")
    await _wait_until(lambda: client.status[(RecordType.AREA, 1)].partial_armed)
    await client.stop()


async def test_bypass_and_outputs(panel):
    client = ProtegeClient("127.0.0.1", panel.port, "1234")
    await client.connect()
    await client.start(await client.discover())
    await client.set_bypass(RecordType.INPUT, 2, True)
    await _wait_until(lambda: client.status[(RecordType.INPUT, 2)].bypassed)
    await client.set_bypass(RecordType.INPUT, 2, False)
    await _wait_until(lambda: not client.status[(RecordType.INPUT, 2)].bypassed)
    await client.output_on(1, seconds=5)
    assert (p.GROUP_OUTPUT, p.OUTPUT_ON_TIMED, 1) in panel.commands
    await client.stop()


async def test_reconnects_after_drop(panel):
    client = ProtegeClient("127.0.0.1", panel.port, "1234", reconnect_interval=0.05)
    await client.connect()
    await client.start(await client.discover())
    transitions = []
    client.add_connection_listener(transitions.append)
    for s in list(panel.sessions):
        s.writer.close()
    await _wait_until(lambda: transitions[:2] == [False, True])
    await _wait_until(lambda: client.available and panel.sessions and panel.sessions[0].monitored)
    await client.stop()


async def test_event_client_receives_events(panel):
    events = []
    client = ProtegeEventClient("127.0.0.1", panel.port, "4321")
    client.add_listener(events.append)
    await client.open()
    await client.start()
    panel.push_event("Thu 10:48:54 User Warren Entry Granted D2 - Cafe")
    await _wait_until(lambda: events)
    assert events[0].text.endswith("Cafe")
    await client.stop()
