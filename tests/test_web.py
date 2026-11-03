"""Web-only mode, against a fake of the WX web interface."""

from __future__ import annotations

import asyncio

import aiohttp
import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ict_automation.codes import make_code, match_code
from custom_components.ict_automation.const import (
    CODE_MODE_HA,
    CODE_MODE_NONE,
    CONF_CODE_MODE,
    CONF_CODES,
    CONF_CONNECTION,
    CONF_CONTROL_PORT,
    CONF_PIN,
    CONNECTION_ACS,
    CONNECTION_WEB,
    DOMAIN,
)
from custom_components.ict_automation.protege import (
    AreaState,
    DoorLockState,
    InputState,
    RecordType,
    web,
)
from custom_components.ict_automation.protege import protocol as p
from custom_components.ict_automation.protege.web import (
    ProtegeWebClient,
    WebAuthError,
    parse_events,
    parse_status,
    record_from_label,
)

from .fake_web import PASSWORD, USERNAME, FakeWebController


@pytest.fixture
async def controller(socket_enabled, aiohttp_server, monkeypatch):
    monkeypatch.setattr(web, "SCHEME", "http")
    fake = FakeWebController()
    server = await aiohttp_server(fake.app())
    fake.port = server.port
    return fake


async def _settle(hass: HomeAssistant, predicate, timeout: float = 3.0) -> None:
    async def loop():
        while not predicate():
            await asyncio.sleep(0.02)
            await hass.async_block_till_done()

    await asyncio.wait_for(loop(), timeout)


# ----- decoding -------------------------------------------------------------


def test_status_decoding_maps_web_codes_onto_acs_ones():
    doors = parse_status(RecordType.DOOR, "Door0=9,4,0&Door1=12,0,0&Door2147483647=0,0", {})
    assert doors[0].lock_state is DoorLockState.UNLOCKED_LATCHED  # unlocked by operator, latched
    assert doors[0].door_state == 4  # forced open
    assert doors[1].locked  # locked by a calendar action
    assert 2147483647 not in doors

    areas = parse_status(RecordType.AREA, "Area16=0,128,3,0", {})
    assert areas[16].state is AreaState.ARMED
    assert areas[16].alarm and areas[16].siren

    inputs = parse_status(RecordType.INPUT, "Input0=2,0&Input1=3,5", {0: "RE01:I01"})
    assert inputs[0].state is InputState.TAMPER and inputs[0].tampered
    assert inputs[0].reference == "RE01:I01"
    assert inputs[1].state is InputState.SHORT
    assert inputs[1].bypassed and inputs[1].siren_lockout and not inputs[1].bypass_latched


def test_events_are_oldest_first_without_timestamps_or_card_numbers():
    reply = (
        "Event0=Tue 04/11/2026 07:31:00 Door Front Door Unlocked By REX"
        "&EventCodes0=1&Event1=Tue 04/11/2026 07:30:00 User Mikey Entry Granted Front Door [Card 321:5]"
    )
    first, second = parse_events(reply)
    assert first.text == "User Mikey Entry Granted Front Door"
    assert first.user == "Mikey"
    assert second.category == "Door"


def test_module_reference_comes_from_the_label():
    record = record_from_label(RecordType.INPUT, 14, "Gallery PIR - IE01.I01")
    assert (record.reference, record.module, record.name) == ("IE01:I01", "IE01", "Gallery PIR - IE01.I01")
    assert record_from_label(RecordType.OUTPUT, 112, "Controller Bell 0").module is None


def test_codes_are_hashed_and_matched():
    codes = [make_code("Jordan", "1234"), make_code("Tim", "9876")]
    assert "1234" not in str(codes)
    assert match_code(codes, "9876") == "Tim"
    assert match_code(codes, "0000") is None
    assert match_code(codes, None) is None


# ----- client ---------------------------------------------------------------


@pytest.fixture
async def client(controller):
    async with aiohttp.ClientSession() as session:
        client = ProtegeWebClient(
            session,
            "127.0.0.1",
            USERNAME,
            PASSWORD,
            controller.port,
            event_interval=0.05,
            full_refresh_interval=60,
        )
        yield client
        await client.stop()


async def _wait(predicate, timeout: float = 3.0) -> None:
    async def loop():
        while not predicate():
            await asyncio.sleep(0.02)

    await asyncio.wait_for(loop(), timeout)


async def test_discovery_reads_names_and_zone_settings(client, controller):
    info = await client.connect()
    assert info.serial_str == "C227FCC1"
    records = {r.key: r for r in await client.discover()}
    reed = records[(RecordType.INPUT, 0)]
    assert reed.name == "Front Door Reed - RE01.I01"
    assert (reed.areas, reed.arm_open) == ([16], [])
    pir = records[(RecordType.INPUT, 14)]
    assert pir.arm_open == [16]  # Delay No Test
    assert len(records) == 8
    assert controller.logins == 1


async def test_events_drive_status_reads(client, controller):
    await client.connect()
    await client.start(await client.discover())
    await _wait(lambda: client.available and (RecordType.INPUT, 0) in client.status)
    events = []
    client.add_listener(events.append)
    controller.requests.clear()
    await asyncio.sleep(0.2)
    # Nothing happened: only the cheap event check, no status tables.
    assert not any("Type=Status" in q for q in controller.requests)

    controller.inputs[0] = [1, 0]
    controller.log("Input Front Door Reed - RE01.I01 Opened")
    await _wait(lambda: client.status[(RecordType.INPUT, 0)].active)
    assert events[0].category == "Input"
    assert any("SubType=GXT_INPUTS_TBL" in q and "Status" in q for q in controller.requests)
    assert controller.logins == 1  # one session throughout


async def test_commands_and_assumed_state(client, controller):
    await client.connect()
    await client.start(await client.discover())
    await _wait(lambda: client.available)
    await client.door_unlock_latched(0)
    assert client.status[(RecordType.DOOR, 0)].lock_state is DoorLockState.UNLOCKED_LATCHED
    await client.area_command(16, p.AREA_ARM_STAY)
    await client.output_on(112, seconds=30)
    await client.set_bypass(RecordType.INPUT, 14, True, permanent=True)
    assert controller.controls == [
        ("GXT_DOORS_TBL", 0, 2, None),
        ("GXT_AREAS_TBL", 16, 10, None),
        ("GXT_PGMS_TBL", 112, 2, 30),
        ("GXT_INPUTS_TBL", 14, 2, None),
    ]
    assert client.status[(RecordType.AREA, 16)].partial_armed
    assert client.status[(RecordType.INPUT, 14)].bypass_latched

    controller.refuse_controls = True
    with pytest.raises(web.CommandRejected):
        await client.door_lock(0)


async def test_dropped_session_logs_in_again_once(client, controller):
    await client.connect()
    await client.start(await client.discover())
    await _wait(lambda: client.available)
    controller.drop_sessions()
    controller.inputs[14] = [1, 0]
    await client.refresh(RecordType.INPUT)
    assert client.status[(RecordType.INPUT, 14)].active
    assert controller.logins == 2


async def test_rejected_password_stops_logging_in(controller):
    async with aiohttp.ClientSession() as session:
        client = ProtegeWebClient(session, "127.0.0.1", USERNAME, "wrong", controller.port)
        with pytest.raises(WebAuthError):
            await client.connect()
        with pytest.raises(WebAuthError):
            await client.connect()
    assert controller.failed_logins == 1


async def test_logins_are_rate_limited(client, controller):
    for _ in range(web.MAX_LOGINS_PER_WINDOW):
        controller.drop_sessions()
        await client.connect()
    controller.drop_sessions()
    with pytest.raises(web.CannotConnect):
        await client.connect()
    assert controller.logins == web.MAX_LOGINS_PER_WINDOW


async def test_saved_session_is_resumed(client, controller):
    await client.connect()
    cookie = client._wx.cookie
    async with aiohttp.ClientSession() as session:
        again = ProtegeWebClient(session, "127.0.0.1", USERNAME, PASSWORD, controller.port)
        again.resume(cookie)
        await again.connect()
    assert controller.logins == 1


async def test_stop_logs_out(client, controller):
    await client.connect()
    await client.start(await client.discover())
    await _wait(lambda: client.available)
    await client.stop()
    assert not controller.sessions


# ----- Home Assistant ---------------------------------------------------------


def _entry(controller, options=None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id="C227FCC1",
        data={
            CONF_HOST: "127.0.0.1",
            CONF_PORT: controller.port,
            CONF_USERNAME: USERNAME,
            CONF_PASSWORD: PASSWORD,
            CONF_CONNECTION: CONNECTION_WEB,
        },
        options=options or {},
    )


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch):
    original = ProtegeWebClient.__init__

    def init(self, *args, **kwargs):
        kwargs.setdefault("event_interval", 0.05)
        original(self, *args, **kwargs)

    monkeypatch.setattr(ProtegeWebClient, "__init__", init)


async def _setup(hass, entry):
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_config_flow_web(hass: HomeAssistant, controller) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["step_id"] == "user"
    # Nothing answers on the control port (1), so it carries on through the web interface.
    data = {CONF_HOST: "127.0.0.1", CONF_PORT: controller.port, CONF_USERNAME: USERNAME, CONF_CONTROL_PORT: 1}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**data, CONF_PASSWORD: "wrong"}
    )
    assert result["errors"] == {"base": "web_invalid_auth"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**data, CONF_PASSWORD: PASSWORD}
    )
    assert result["step_id"] == "web"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_CODE_MODE: CODE_MODE_HA})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.unique_id == "C227FCC1"
    assert entry.data[CONF_CONNECTION] == CONNECTION_WEB
    assert entry.options == {CONF_CODE_MODE: CODE_MODE_HA}
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.soundline_ready_to_arm") is not None

    # Options: add a person's code.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "codes"})
    assert result["step_id"] == "codes"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_CODE_MODE: CODE_MODE_HA, "add_name": "Jordan", "add_code": "12"}
    )
    assert result["errors"] == {"add_code": "code_too_short"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_CODE_MODE: CODE_MODE_HA, "add_name": "Jordan", "add_code": "2468"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert [c["name"] for c in entry.options[CONF_CODES]] == ["Jordan"]
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert not controller.sessions  # logged out on unload


async def test_web_entities_and_codes(hass: HomeAssistant, controller) -> None:
    entry = _entry(controller, {CONF_CODE_MODE: CODE_MODE_HA, CONF_CODES: [make_code("Jordan", "2468")]})
    await _setup(hass, entry)
    area = "alarm_control_panel.soundline"
    assert hass.states.get(area).state == "disarmed"
    assert hass.states.get("lock.front_door").state == "locked"
    assert hass.states.get("event.protege_wx_panel_event") is not None

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "alarm_control_panel", "alarm_arm_away", {"entity_id": area, "code": "1111"}, blocking=True
        )
    assert not controller.controls
    await hass.services.async_call(
        "alarm_control_panel", "alarm_arm_away", {"entity_id": area, "code": "2468"}, blocking=True
    )
    assert controller.controls == [("GXT_AREAS_TBL", 16, 3, None)]
    await _settle(hass, lambda: hass.states.get(area).state == "armed_away")
    assert hass.states.get(area).attributes["changed_by"] == "Jordan"

    # A keypad change shows up through the event log.
    controller.doors[1][0] = 5
    controller.log("Door Jail Gate Unlocked By REX")
    await _settle(hass, lambda: hass.states.get("lock.jail_gate").state == "unlocked")
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_web_without_codes(hass: HomeAssistant, controller) -> None:
    entry = _entry(controller, {CONF_CODE_MODE: CODE_MODE_NONE})
    await _setup(hass, entry)
    area = "alarm_control_panel.soundline"
    assert hass.states.get(area).attributes.get("code_format") is None
    await hass.services.async_call(
        "alarm_control_panel", "alarm_arm_home", {"entity_id": area}, blocking=True
    )
    assert controller.controls == [("GXT_AREAS_TBL", 16, 10, None)]
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_rejected_operator_asks_to_reauthenticate(hass: HomeAssistant, controller) -> None:
    entry = _entry(controller)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_PASSWORD: "wrong"})
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flows and flows[0]["context"]["source"] == config_entries.SOURCE_REAUTH
    assert controller.failed_logins == 1


@pytest.mark.parametrize("display_order", [False, True])
async def test_upgrade_to_control_service_keeps_entities(
    hass: HomeAssistant, controller, display_order: bool
) -> None:
    """A web-only install finds the control service later and moves onto it, keeping its entities.

    By default the control service numbers records by record ID, like the web interface;
    with ACPUseDisplayOrder it numbers them 1, 2, 3 and the entities are renumbered.
    """
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers import issue_registry as ir

    from .fake_panel import FakePanel

    panel = FakePanel()
    # The same panel as the fake web interface.
    panel.serial = 0xC227FCC1
    if display_order:
        panel.doors = {1: [0, 0], 2: [0, 0]}
        panel.areas = {1: [0, 0, 0], 2: [0, 0, 0]}
        panel.inputs = {1: ["ZX001:01", 0, 0], 2: ["ZX002:01", 0, 0]}
        panel.outputs = {1: ["CP001:01", 0]}
        panel.troubles = {1: ["CP001:01", 0, 0]}
    else:
        panel.doors = {0: [0, 0], 1: [0, 0]}
        panel.areas = {0: [0, 0, 0], 16: [0, 0, 0]}
        panel.inputs = {0: ["ZX001:01", 0, 0], 14: ["ZX002:01", 0, 0]}
        panel.outputs = {112: ["CP001:01", 0]}
        panel.troubles = {191: ["CP001:01", 0, 0]}
    await panel.start()
    try:
        entry = _entry(controller, {CONF_CODE_MODE: CODE_MODE_NONE})
        entry.add_to_hass(hass)
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_CONTROL_PORT: panel.port})
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        issues = ir.async_get(hass)
        await _settle(
            hass, lambda: issues.async_get_issue(DOMAIN, f"acs_available_{entry.entry_id}") is not None
        )

        ent_reg = er.async_get(hass)
        before = {e.entity_id for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)}
        gallery = next(e for e in before if e.startswith("binary_sensor.gallery_pir"))
        assert ent_reg.async_get(gallery).unique_id.startswith("C227FCC1_input_14_")

        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.MENU
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "upgrade"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_PORT: panel.port, CONF_PIN: "0000"}
        )
        assert result["errors"] == {CONF_PIN: "invalid_pin"}
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_PORT: panel.port, CONF_PIN: "1234"}
        )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "upgraded"
        await hass.async_block_till_done()

        assert entry.data[CONF_CONNECTION] == CONNECTION_ACS
        assert entry.options["wx_username"] == USERNAME
        assert issues.async_get_issue(DOMAIN, f"acs_available_{entry.entry_id}") is None
        assert not controller.sessions  # the web session was closed
        new_index = 2 if display_order else 14
        assert ent_reg.async_get(gallery).unique_id.startswith(f"C227FCC1_input_{new_index}_")
        after = {e.entity_id for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)}
        assert before <= after
        assert hass.states.get("lock.front_door").state == "locked"
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    finally:
        await panel.stop()
