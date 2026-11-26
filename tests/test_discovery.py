"""Discovery and status reads on panels that number records by record ID (as a GX does)."""

from __future__ import annotations

import asyncio
import logging
import struct

import pytest

from custom_components.ict_automation.config_flow import format_id_list, parse_id_list
from custom_components.ict_automation.protege import ProtegeClient, Record, RecordType
from custom_components.ict_automation.protege import protocol as p
from custom_components.ict_automation.protege.client import NUMBERING_RECORD_ID
from custom_components.ict_automation.protege.models import AreaState, AreaStatus, decode_status


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    async def loop():
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(loop(), timeout)


@pytest.fixture
def gx(panel):
    """Record IDs with holes: door 0, inputs past an empty run, outputs from 19."""
    panel.doors = {0: [0, 0], 1: [0, 0]}
    panel.areas = {0: [0, 0, 0], 1: [0x80, 0, 0x01]}
    panel.inputs = {0: ["CP001:01", 0, 0], 1: ["CP001:02", 0, 0], 12: ["ZX001:01", 0, 0]}
    panel.outputs = {19: ["CP001:01", 0], 20: ["CP001:02", 0]}
    panel.troubles = {0: ["CP001:01", 0, 0]}
    return panel


async def _client(panel, **kwargs) -> ProtegeClient:
    client = ProtegeClient("127.0.0.1", panel.port, "1234", **kwargs)
    await client.connect()
    return client


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (bytes((0x80, 0x80, 0x01, 0x00)), (AreaState.ARMED, 0x80, 0x01)),  # the documented 8 bytes
        (bytes((0x80, 0x80, 0x01)), (AreaState.ARMED, 0x80, 0x01)),
        (bytes((0x80, 0x01)), (AreaState.ARMED, 0x01, 0x00)),
        (bytes((0x80,)), (AreaState.ARMED, 0x00, 0x00)),
    ],
)
def test_short_area_status_blocks_are_read(body: bytes, expected) -> None:
    block = p.ICTDataBlock(0x00, p.GROUP_AREA, struct.pack("<I", 3) + body)
    update = decode_status(block)
    assert update is not None and update.index == 3
    assert update.status == AreaStatus(*expected)


def test_door_status_still_needs_its_position_byte() -> None:
    assert decode_status(p.ICTDataBlock(0x00, p.GROUP_DOOR, struct.pack("<I", 1) + b"\x00")) is None


async def test_scan_starts_at_zero_and_crosses_holes(gx) -> None:
    client = await _client(gx)
    records = await client.discover()
    assert {r.key for r in records} == {
        (RecordType.AREA, 0),
        (RecordType.AREA, 1),
        (RecordType.DOOR, 0),
        (RecordType.DOOR, 1),
        (RecordType.OUTPUT, 19),
        (RecordType.OUTPUT, 20),
        (RecordType.INPUT, 0),
        (RecordType.INPUT, 1),
        (RecordType.INPUT, 12),
        (RecordType.TROUBLE_INPUT, 0),
    }
    assert client.numbering == NUMBERING_RECORD_ID
    assert not client.unanswered
    await client.disconnect()


async def test_gap_setting_and_extra_ids(gx) -> None:
    gx.outputs[300] = ["CP001:03", 0]
    client = await _client(gx)
    keys = {r.key for r in await client.discover(gap_tolerance=5)}
    assert (RecordType.INPUT, 12) not in keys  # 10 empty IDs in a row
    assert (RecordType.OUTPUT, 19) not in keys
    # Saved or pinned IDs are read even past a gap.
    keys = {r.key for r in await client.discover(gap_tolerance=5, extra=[(RecordType.OUTPUT, 300)])}
    assert (RecordType.OUTPUT, 300) in keys
    # 0 scans every ID up to the limit.
    limits = dict.fromkeys(RecordType, 30)
    keys = {r.key for r in await client.discover(limits, gap_tolerance=0)}
    assert {(RecordType.INPUT, 12), (RecordType.OUTPUT, 19), (RecordType.OUTPUT, 20)} <= keys
    await client.disconnect()


async def test_no_answer_is_not_an_empty_id(gx) -> None:
    gx.silent = {(p.GROUP_DOOR, 1)}
    client = await _client(gx)
    keys = {r.key for r in await client.discover(probe_timeout=0.1)}
    assert (RecordType.DOOR, 0) in keys
    assert (RecordType.DOOR, 1) not in keys
    assert client.unanswered == {(RecordType.DOOR, 1)}
    await client.disconnect()


async def test_scan_gives_up_when_the_panel_stops_answering(gx, caplog) -> None:
    gx.silent = {(p.GROUP_INPUT, i) for i in range(2, 40)}
    client = await _client(gx)
    with caplog.at_level(logging.WARNING):
        keys = {r.key for r in await client.discover(probe_timeout=0.05)}
    assert (RecordType.INPUT, 12) not in keys
    assert client.unanswered == {(RecordType.INPUT, i) for i in (2, 3, 4)}
    assert "did not answer 3 requests in a row" in caplog.text
    # The other types are still scanned.
    assert (RecordType.OUTPUT, 19) in keys
    await client.disconnect()


@pytest.mark.parametrize("refusal", ["nack", "silent"])
async def test_refused_area_status_is_read_through_monitoring(gx, refusal: str) -> None:
    if refusal == "nack":
        gx.status_nack = {p.GROUP_AREA: p.NACK_SERVICE_COMMAND_NOT_VALID}
    else:
        gx.status_silent = {p.GROUP_AREA}
    client = await _client(gx)
    records = await client.discover(probe_timeout=0.2)
    assert {r.index for r in records if r.record_type is RecordType.AREA} == {0, 1}
    assert client.status[(RecordType.AREA, 1)].armed
    # Discovery leaves no subscriptions behind.
    assert not gx.sessions[0].monitored
    # Later reads go straight to monitoring.
    gx.areas[1] = [0x00, 0, 0]
    await client.request_status(RecordType.AREA, 1)
    assert not client.status[(RecordType.AREA, 1)].armed
    await client.disconnect()


async def test_refresh_carries_on_past_a_record_that_does_not_answer(gx) -> None:
    client = await _client(gx, timeout=0.1)
    records = await client.discover()
    client.set_records(records)
    gx.silent = {(p.GROUP_DOOR, 0)}
    gx.inputs[12][1] = 1  # changed without a push
    await client.refresh_all()
    assert client.status[(RecordType.INPUT, 12)].active
    await client.disconnect()


async def test_records_monitoring_did_not_report_are_read_after_start(gx) -> None:
    gx.force_update = False
    client = await _client(gx, poll_interval=3600)
    records = await client.discover()
    client.status.clear()
    await client.start(records)
    await _wait_until(lambda: all(r.key in client.status for r in records))
    await client.stop()


def test_record_id_lists() -> None:
    assert parse_id_list("0, 12-14; 19=Garage light\n7 = ") == [
        (0, None),
        (7, None),
        (12, None),
        (13, None),
        (14, None),
        (19, "Garage light"),
    ]
    assert parse_id_list("") == []
    for bad in ("x", "-1", "5-2", "1-3=Name"):
        with pytest.raises(ValueError):
            parse_id_list(bad)
    records = [Record(RecordType.OUTPUT, i) for i in (0, 12, 13, 14)] + [
        Record(RecordType.OUTPUT, 19, name="Garage light")
    ]
    assert format_id_list(records) == "0, 12-14, 19=Garage light"
    assert parse_id_list(format_id_list(records)) == [(r.index, r.name) for r in records]
