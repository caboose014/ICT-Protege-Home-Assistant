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

from custom_components.ict_automation.const import CONF_PIN, DOMAIN
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
    assert names == {"Protege WX", "Door 1", "Door 2", "Area 1", "ZX001"}
    controller = dev_reg.async_get_device(identifiers={(DOMAIN, "00A1B2C3")})
    assert controller.sw_version == "WX 4.00.1676"
    door = dev_reg.async_get_device(identifiers={(DOMAIN, "00A1B2C3_door_1")})
    assert door.via_device_id == controller.id

    assert hass.states.get("lock.door_1").state == "locked"
    assert hass.states.get("binary_sensor.door_1_contact").state == "off"
    assert hass.states.get("sensor.door_1_door_state").state == "closed"
    assert hass.states.get("alarm_control_panel.area_1").state == "disarmed"
    assert hass.states.get("binary_sensor.protege_wx_input_cp001_01").state == "off"
    assert hass.states.get("binary_sensor.zx001_input_zx001_01").state == "off"
    assert hass.states.get("switch.protege_wx_output_cp001_01").state == "off"
    assert hass.states.get("switch.protege_wx_input_cp001_02_bypass").state == "off"
    ent_reg = er.async_get(hass)
    trouble = ent_reg.async_get("binary_sensor.protege_wx_trouble_input_cp001_01")
    assert trouble.disabled_by is er.RegistryEntryDisabler.INTEGRATION


async def test_input_push_update(hass: HomeAssistant, entry, panel: FakePanel) -> None:
    panel.set_input(3, 1)
    await _settle(hass, lambda: hass.states.get("binary_sensor.zx001_input_zx001_01").state == "on")


async def test_lock_unlock_and_open(hass: HomeAssistant, entry, panel: FakePanel) -> None:
    await hass.services.async_call("lock", "unlock", {"entity_id": "lock.door_1"}, blocking=True)
    await _settle(hass, lambda: hass.states.get("lock.door_1").state == "unlocked")
    assert (p.GROUP_DOOR, p.DOOR_UNLOCK_LATCHED, 1) in panel.commands
    await hass.services.async_call("lock", "lock", {"entity_id": "lock.door_1"}, blocking=True)
    await _settle(hass, lambda: hass.states.get("lock.door_1").state == "locked")
    await hass.services.async_call("lock", "open", {"entity_id": "lock.door_2"}, blocking=True)
    assert (p.GROUP_DOOR, p.DOOR_UNLOCK_MOMENTARY, 2) in panel.commands


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
        "switch", "turn_on", {"entity_id": "switch.protege_wx_output_cp001_01"}, blocking=True
    )
    await _settle(hass, lambda: hass.states.get("switch.protege_wx_output_cp001_01").state == "on")
    await hass.services.async_call(
        DOMAIN,
        "turn_on_for",
        {"entity_id": "switch.protege_wx_output_cp001_02", "duration": 30},
        blocking=True,
    )
    assert (p.GROUP_OUTPUT, p.OUTPUT_ON_TIMED, 2) in panel.commands
    await hass.services.async_call(
        DOMAIN,
        "bypass",
        {"entity_id": "switch.protege_wx_input_cp001_02_bypass", "permanent": True},
        blocking=True,
    )
    await _settle(hass, lambda: hass.states.get("switch.protege_wx_input_cp001_02_bypass").state == "on")
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
