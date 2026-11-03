"""Client tests against the simulated panel."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.ict_automation.protege import (
    CommandRejected,
    DoorLockState,
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
    await _wait_until(lambda: (p.GROUP_INPUT, 3) in session.monitored)

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


async def test_debug_log_never_contains_the_pin(panel, caplog):
    import logging

    caplog.set_level(logging.DEBUG, logger="custom_components.ict_automation.protege")
    client = ProtegeClient("127.0.0.1", panel.port, "1234")
    await client.connect()
    await client.start(await client.discover())
    await client.door_lock(1)
    await client.stop()
    assert "login [PIN redacted]" in caplog.text
    assert "00 02 01 02 03 04" not in caplog.text


async def test_command_shows_assumed_state_before_panel_reports(panel):
    """The real panel doesn't push changes made while logged in, so HA shows the result straight away."""
    client = ProtegeClient("127.0.0.1", panel.port, "1234")
    await client.connect()
    records = await client.discover()
    await client.start(records)
    panel.push = lambda group, index: None  # panel stays silent
    panel.force_update = False
    updates = []
    client.add_status_listener(updates.append)

    await client.door_unlock_latched(1)
    first_door = next(u for u in updates if u.record_type is RecordType.DOOR and u.index == 1)
    assert first_door.status.lock_state == DoorLockState.UNLOCKED_LATCHED
    assert client.status[(RecordType.DOOR, 1)].lock_state == DoorLockState.UNLOCKED_LATCHED

    await client.set_bypass(RecordType.INPUT, 2, True)
    assert client.status[(RecordType.INPUT, 2)].bypassed
    await client.stop()


async def test_commands_are_not_stuck_behind_monitoring_restore(panel, monkeypatch):
    """Re-registering monitors is slow on a real panel; a second command must not wait for all of it."""
    from custom_components.ict_automation.protege import client as client_mod

    monkeypatch.setattr(client_mod, "MONITOR_BATCH", 2)
    client = ProtegeClient("127.0.0.1", panel.port, "1234")
    await client.connect()
    records = await client.discover()
    await client.start(records)
    panel.monitor_delay = 0.1  # 9 records -> ~0.9 s to restore everything

    await client.door_unlock_latched(1)
    await asyncio.sleep(0.15)  # restore is now under way
    loop = asyncio.get_running_loop()
    started = loop.time()
    await client.door_lock(1)
    assert loop.time() - started < 0.6
    assert client.status[(RecordType.DOOR, 1)].locked

    # Monitoring still ends up fully restored after the last command.
    session = panel.sessions[0]
    await _wait_until(lambda: len(session.monitored) == len(records), timeout=3)
    await client.stop()


async def test_badge_unlock_shows_even_when_panel_only_pushes_the_door_opening(panel, monkeypatch):
    """Seen on site: a granted badge pushed the door opening with the lock byte still 'locked'."""
    from custom_components.ict_automation.protege import client as client_mod

    monkeypatch.setattr(client_mod, "DOOR_WATCH_DELAYS", (0.05, 0.2, 0.4))
    client = ProtegeClient("127.0.0.1", panel.port, "1234")
    await client.connect()
    records = await client.discover()
    await client.start(records)

    panel.doors[1] = [0x01, 0x01]  # the panel's real state: unlocked by user, door open
    stale = p.build_data_packet([((0x00, p.GROUP_DOOR), bytes((1, 0, 0, 0, 0x00, 0x01, 0, 0)))])
    panel.sessions[0].writer.write(stale)  # the push only reports the door opening
    await _wait_until(lambda: client.status[(RecordType.DOOR, 1)].lock_state == DoorLockState.UNLOCKED_USER)

    panel.doors[1] = [0x00, 0x00]  # relocks on its timer without a push
    await _wait_until(lambda: client.status[(RecordType.DOOR, 1)].locked)
    await client.stop()


async def test_pushed_status_is_acked_when_panel_refuses_ack_config(panel):
    """The panel resends unacked pushes and can hold back later ones."""
    panel.accept_ack_config = False
    client = ProtegeClient("127.0.0.1", panel.port, "1234")
    await client.connect()
    records = await client.discover()
    await client.start(records)
    before = panel.acks_received
    panel.set_input(3, 1)
    await _wait_until(lambda: panel.acks_received > before)
    await client.stop()


async def test_ack_config_turns_off_push_acks(panel):
    client = ProtegeClient("127.0.0.1", panel.port, "1234")
    await client.connect()
    records = await client.discover()
    await client.start(records)
    assert panel.sessions[0].expects_acks is False
    before = panel.acks_received
    panel.set_input(3, 1)
    await _wait_until(lambda: client.status[(RecordType.INPUT, 3)].active)
    await asyncio.sleep(0.1)
    assert panel.acks_received == before
    await client.stop()


async def test_event_block_accepted_in_either_byte_order(panel):
    panel.accept_ack_config = False
    events = []
    client = ProtegeEventClient("127.0.0.1", panel.port, "4321")
    client.add_listener(events.append)
    await client.open()
    raw = p.build_data_packet([((0x30, 0x01), b"Thu 10:48:54 User Warren Entry Granted D2\x00")])
    panel.sessions[0].writer.write(raw)
    await _wait_until(lambda: events)
    assert events[0].text.startswith("Thu 10:48:54")
    await _wait_until(lambda: panel.acks_received > 0)
    await client.stop()
