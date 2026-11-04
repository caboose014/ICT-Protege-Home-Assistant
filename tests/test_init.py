"""Setup, entity and command tests against the simulated panel."""

from __future__ import annotations

import asyncio

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ict_automation.const import CONF_PIN, CONF_RECORDS, DOMAIN
from custom_components.ict_automation.protege import protocol as p

from .fake_panel import FakePanel


async def _settle(hass: HomeAssistant, predicate, timeout: float = 3.0) -> None:
    async def loop():
        while not predicate():
            await asyncio.sleep(0.02)
            await hass.async_block_till_done()

    await asyncio.wait_for(loop(), timeout)


@pytest.fixture
async def entry(hass: HomeAssistant, panel: FakePanel):
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id="00A1B2C3",
        data={CONF_HOST: "127.0.0.1", CONF_PORT: panel.port, CONF_PIN: "1234"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    yield entry
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_devices_and_entities(hass: HomeAssistant, entry) -> None:
    assert entry.state is ConfigEntryState.LOADED
    dev_reg = dr.async_get(hass)
    names = {d.name for d in dr.async_entries_for_config_entry(dev_reg, entry.entry_id)}
    assert names == {
        "Protege WX",
        "Door 1",
        "Door 2",
        "Area 1",
        "ZX001",
        "Input CP001:01",
        "Input CP001:02",
        "Input ZX001:01",
        "Output CP001:01",
        "Output CP001:02",
    }
    controller = dev_reg.async_get_device(identifiers={(DOMAIN, "00A1B2C3")})
    assert controller.sw_version == "WX 4.00.1676"
    door = dev_reg.async_get_device(identifiers={(DOMAIN, "00A1B2C3_door_1")})
    assert door.via_device_id == controller.id
    # Inputs and outputs are their own devices, so each can go in its own HA area.
    zx_module = dev_reg.async_get_device(identifiers={(DOMAIN, "00A1B2C3_module_ZX001")})
    zx_input = dev_reg.async_get_device(identifiers={(DOMAIN, "00A1B2C3_input_3")})
    assert zx_module.via_device_id == controller.id
    assert zx_input.via_device_id == zx_module.id
    cp_output = dev_reg.async_get_device(identifiers={(DOMAIN, "00A1B2C3_output_1")})
    assert cp_output.via_device_id == controller.id

    assert hass.states.get("lock.door_1").state == "locked"
    assert hass.states.get("binary_sensor.door_1_contact").state == "off"
    assert hass.states.get("sensor.door_1_door_state").state == "closed"
    assert hass.states.get("alarm_control_panel.area_1").state == "disarmed"
    assert hass.states.get("binary_sensor.input_cp001_01").state == "off"
    assert hass.states.get("binary_sensor.input_zx001_01").state == "off"
    assert hass.states.get("switch.output_cp001_01").state == "off"
    assert hass.states.get("switch.input_cp001_02_bypass").state == "off"
    ent_reg = er.async_get(hass)
    trouble = ent_reg.async_get("binary_sensor.protege_wx_trouble_input_cp001_01")
    assert trouble.disabled_by is er.RegistryEntryDisabler.INTEGRATION


async def test_input_push_update(hass: HomeAssistant, entry, panel: FakePanel) -> None:
    panel.set_input(3, 1)
    await _settle(hass, lambda: hass.states.get("binary_sensor.input_zx001_01").state == "on")


async def test_lock_unlock_and_open(hass: HomeAssistant, entry, panel: FakePanel) -> None:
    await hass.services.async_call("lock", "unlock", {"entity_id": "lock.door_1"}, blocking=True)
    await _settle(hass, lambda: hass.states.get("lock.door_1").state == "unlocked")
    assert (p.GROUP_DOOR, p.DOOR_UNLOCK_LATCHED, 1) in panel.commands
    await hass.services.async_call("lock", "lock", {"entity_id": "lock.door_1"}, blocking=True)
    await _settle(hass, lambda: hass.states.get("lock.door_1").state == "locked")
    await hass.services.async_call("lock", "open", {"entity_id": "lock.door_2"}, blocking=True)
    assert (p.GROUP_DOOR, p.DOOR_UNLOCK_MOMENTARY, 2) in panel.commands
    panel.commands.clear()
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.door_1_momentary_unlock"}, blocking=True
    )
    assert panel.commands == [(p.GROUP_DOOR, p.DOOR_UNLOCK_MOMENTARY, 1)]


async def test_arming_uses_the_persons_code(hass: HomeAssistant, entry, panel: FakePanel) -> None:
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "alarm_control_panel",
            "alarm_arm_away",
            {"entity_id": "alarm_control_panel.area_1"},
            blocking=True,
        )
    with pytest.raises(HomeAssistantError, match="rights for this area"):
        await hass.services.async_call(
            "alarm_control_panel",
            "alarm_arm_away",
            {"entity_id": "alarm_control_panel.area_1", "code": "1234"},
            blocking=True,
        )
    await hass.services.async_call(
        "alarm_control_panel",
        "alarm_arm_home",
        {"entity_id": "alarm_control_panel.area_1", "code": "5678"},
        blocking=True,
    )
    await _settle(hass, lambda: hass.states.get("alarm_control_panel.area_1").state == "armed_home")
    await hass.services.async_call(
        "alarm_control_panel",
        "alarm_disarm",
        {"entity_id": "alarm_control_panel.area_1", "code": "5678"},
        blocking=True,
    )
    await _settle(hass, lambda: hass.states.get("alarm_control_panel.area_1").state == "disarmed")


async def test_outputs_and_bypass(hass: HomeAssistant, entry, panel: FakePanel) -> None:
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.output_cp001_01"}, blocking=True
    )
    await _settle(hass, lambda: hass.states.get("switch.output_cp001_01").state == "on")
    await hass.services.async_call(
        DOMAIN,
        "turn_on_for",
        {"entity_id": "switch.output_cp001_02", "duration": 30},
        blocking=True,
    )
    assert (p.GROUP_OUTPUT, p.OUTPUT_ON_TIMED, 2) in panel.commands
    await hass.services.async_call(
        DOMAIN,
        "bypass",
        {"entity_id": "switch.input_cp001_02_bypass", "permanent": True},
        blocking=True,
    )
    await _settle(hass, lambda: hass.states.get("switch.input_cp001_02_bypass").state == "on")
    assert (p.GROUP_INPUT, p.INPUT_BYPASS_PERMANENT, 2) in panel.commands


async def test_unavailable_when_panel_drops(hass: HomeAssistant, entry, panel: FakePanel) -> None:
    await panel.stop()
    await _settle(hass, lambda: hass.states.get("lock.door_1").state == "unavailable")


async def test_migrates_v1_entry(hass: HomeAssistant, panel: FakePanel) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data={CONF_HOST: "127.0.0.1", CONF_PORT: panel.port, "password": "1234"},
        options={"doors": {"1": "Front"}},
    )
    entry.add_to_hass(hass)
    ent_reg = er.async_get(hass)
    ent_reg.async_get_or_create("lock", DOMAIN, "ict_door_1", config_entry=entry)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.version == 2
    assert entry.data[CONF_PIN] == "1234"
    assert entry.unique_id == "00A1B2C3"
    assert ent_reg.async_get_entity_id("lock", DOMAIN, "ict_door_1") is None
    assert hass.states.get("lock.door_1") is not None
    await hass.config_entries.async_unload(entry.entry_id)


async def test_lock_state_updates_without_force_update(hass: HomeAssistant, panel: FakePanel) -> None:
    """Real panels may ignore the monitoring 'force update' flag; state must still follow commands."""
    panel.force_update = False
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id="00A1B2C3",
        data={CONF_HOST: "127.0.0.1", CONF_PORT: panel.port, CONF_PIN: "1234"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await hass.services.async_call("lock", "unlock", {"entity_id": "lock.door_1"}, blocking=True)
    await _settle(hass, lambda: hass.states.get("lock.door_1").state == "unlocked")
    await hass.services.async_call("lock", "lock", {"entity_id": "lock.door_1"}, blocking=True)
    await _settle(hass, lambda: hass.states.get("lock.door_1").state == "locked")
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_arm_modes_can_be_turned_off_per_area(hass: HomeAssistant, entry) -> None:
    panel_id = "alarm_control_panel.area_1"
    assert hass.states.get(panel_id).attributes["supported_features"] == 7
    ent_reg = er.async_get(hass)
    night = ent_reg.async_get_entity_id("switch", DOMAIN, "00A1B2C3_area_1_arm_mode_night")
    assert ent_reg.async_get(night).entity_category is not None  # sits under Configuration

    await hass.services.async_call("switch", "turn_off", {"entity_id": night}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(panel_id).attributes["supported_features"] == 3  # away + home
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "alarm_control_panel",
            "alarm_arm_night",
            {"entity_id": panel_id, "code": "5678"},
            blocking=True,
        )

    # The choice survives a reload.
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(night).state == "off"
    assert hass.states.get(panel_id).attributes["supported_features"] == 3


@pytest.fixture
async def zoned_entry(hass: HomeAssistant, panel: FakePanel):
    """An entry whose inputs' areas were read from the web interface earlier.

    Input 1 must be closed to arm, input 2 may stay open, input 3 is in no area.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id="00A1B2C3",
        data={
            CONF_HOST: "127.0.0.1",
            CONF_PORT: panel.port,
            CONF_PIN: "1234",
            CONF_RECORDS: [
                {
                    "type": "input",
                    "index": 1,
                    "reference": "CP001:01",
                    "name": "Hall PIR",
                    "areas": [1],
                    "arm_open": [],
                },
                {
                    "type": "input",
                    "index": 2,
                    "reference": "CP001:02",
                    "name": "Garage Door",
                    "areas": [1],
                    "arm_open": [1],
                },
                {
                    "type": "input",
                    "index": 3,
                    "reference": "ZX001:01",
                    "name": "Shed",
                    "areas": [],
                    "arm_open": [],
                },
            ],
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    yield entry
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_open_zones_count_only_zones_that_must_close(
    hass: HomeAssistant, zoned_entry, panel: FakePanel
) -> None:
    ready, count = "binary_sensor.area_1_ready_to_arm", "sensor.area_1_open_zones"
    await _settle(hass, lambda: hass.states.get(ready).state == "on")
    assert hass.states.get(count).state == "0"

    panel.set_input(2, 1)  # allowed open when arming
    panel.set_input(3, 1)  # not in the area
    await _settle(hass, lambda: hass.states.get("binary_sensor.hall_pir").state == "off")
    await hass.async_block_till_done()
    assert hass.states.get(ready).state == "on"

    panel.set_input(1, 1)
    await _settle(hass, lambda: hass.states.get(ready).state == "off")
    assert hass.states.get(count).state == "1"
    assert hass.states.get(count).attributes["zones"] == ["Hall PIR"]


async def test_failed_arm_names_the_open_zones(hass: HomeAssistant, zoned_entry, panel: FakePanel) -> None:
    panel.must_close = {1}
    panel.set_input(1, 1)
    await _settle(hass, lambda: hass.states.get("binary_sensor.area_1_ready_to_arm").state == "off")
    with pytest.raises(HomeAssistantError, match="Hall PIR"):
        await hass.services.async_call(
            "alarm_control_panel",
            "alarm_arm_away",
            {"entity_id": "alarm_control_panel.area_1", "code": "5678"},
            blocking=True,
        )
    assert hass.states.get("alarm_control_panel.area_1").state == "disarmed"


async def test_alarm_cause_is_the_zone_that_tripped(
    hass: HomeAssistant, zoned_entry, panel: FakePanel
) -> None:
    cause = "sensor.area_1_alarm_cause"
    assert hass.states.get(cause).state == "unknown"
    panel.areas[1] = [0x80, 0x00, 0x00]
    panel.push(p.GROUP_AREA, 1)
    panel.set_input(1, 1)
    await _settle(hass, lambda: hass.states.get("binary_sensor.hall_pir").state == "on")
    panel.areas[1] = [0x80, 0x00, 0x01]  # alarm
    panel.push(p.GROUP_AREA, 1)
    await _settle(hass, lambda: hass.states.get(cause).state == "Hall PIR")
    panel.set_input(1, 0)
    panel.areas[1] = [0x00, 0x00, 0x00]  # disarmed
    panel.push(p.GROUP_AREA, 1)
    await _settle(hass, lambda: hass.states.get(cause).state == "unknown")


async def test_no_zone_sensors_without_zone_settings(hass: HomeAssistant, entry) -> None:
    assert hass.states.get("binary_sensor.area_1_ready_to_arm") is None
    assert hass.states.get("sensor.area_1_open_zones") is None
    assert hass.states.get("sensor.area_1_alarm_cause") is not None


async def test_duress_raises_a_repair_until_handled(hass: HomeAssistant, panel: FakePanel) -> None:
    from homeassistant.helpers import issue_registry as ir

    from custom_components.ict_automation.const import CONF_EVENTS_PIN, CONF_EVENTS_PORT

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id="00A1B2C3",
        data={CONF_HOST: "127.0.0.1", CONF_PORT: panel.port, CONF_PIN: "1234"},
        options={CONF_EVENTS_PORT: panel.port, CONF_EVENTS_PIN: "4321"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    fired = []
    hass.bus.async_listen(f"{DOMAIN}_emergency", fired.append)
    issues = ir.async_get(hass)

    def emergencies():
        return [i for (domain, _), i in issues.issues.items() if domain == DOMAIN]

    await _settle(hass, lambda: any(s.events for s in panel.sessions))
    panel.push_event("User\x00Jordan Duress Alarm At Keypad 1")
    await _settle(hass, lambda: fired)
    assert fired[0].data["kind"] == "duress"
    [issue] = emergencies()
    assert issue.severity == ir.IssueSeverity.CRITICAL
    assert issue.is_fixable

    panel.push_event("Door\x00Front Door Unlocked By REX")
    await asyncio.sleep(0.2)
    assert len(fired) == 1
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def test_emergency_kinds() -> None:
    from custom_components.ict_automation.hub import emergency_kind

    assert emergency_kind("Area House Panic Alarm") == "panic"
    assert emergency_kind("User Jane Hold-Up") == "panic"
    assert emergency_kind("Keypad 1 Medical Alarm") == "medical"
    assert emergency_kind("Input Garage Opened") is None
    assert emergency_kind(None) is None


async def test_tamper_and_fault(hass: HomeAssistant, entry, panel: FakePanel) -> None:
    tamper, fault = "binary_sensor.input_zx001_01_tamper", "binary_sensor.protege_wx_fault"
    assert hass.states.get(tamper).state == "off"
    assert hass.states.get(fault).state == "off"
    panel.set_input(3, 3)  # tamper
    await _settle(hass, lambda: hass.states.get(tamper).state == "on")
    assert hass.states.get(fault).state == "on"
    assert hass.states.get(fault).attributes["active"] == ["Input ZX001:01 tamper"]
    panel.set_input(3, 0)
    panel.troubles[1][1] = 1
    panel.push(p.GROUP_TROUBLE_INPUT, 1)
    await _settle(hass, lambda: hass.states.get(tamper).state == "off")
    await _settle(hass, lambda: hass.states.get(fault).attributes["active"] == ["Trouble input CP001:01"])
