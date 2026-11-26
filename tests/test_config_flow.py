"""Config and options flow tests."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.ict_automation.const import (
    CONF_CONTROL_PORT,
    CONF_DISCOVERY_GAP,
    CONF_EVENTS_PIN,
    CONF_EVENTS_PORT,
    CONF_PIN,
    CONF_PINNED,
    CONF_RECORDS,
    DOMAIN,
)

from .fake_panel import FakePanel


async def _start_flow(hass: HomeAssistant, control_port: int):
    """Fill in the first step with no web interface reachable (port 1), so only the control service answers."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    return await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_HOST: "127.0.0.1",
            CONF_USERNAME: "homeassistant",
            CONF_PASSWORD: "secret",
            CONF_PORT: 1,
            CONF_CONTROL_PORT: control_port,
        },
    )


async def test_user_flow_discovers_everything(hass: HomeAssistant, panel: FakePanel) -> None:
    result = await _start_flow(hass, panel.port)
    assert result["step_id"] == "acs"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PIN: "1234"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Protege WX (127.0.0.1)"
    assert result["result"].unique_id == "00A1B2C3"
    assert len(result["data"][CONF_RECORDS]) == 9
    await hass.async_block_till_done()


async def test_user_flow_bad_pin(hass: HomeAssistant, panel: FakePanel) -> None:
    result = await _start_flow(hass, panel.port)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PIN: "0000"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_PIN: "invalid_pin"}


async def test_user_flow_cannot_connect(hass: HomeAssistant, socket_enabled) -> None:
    result = await _start_flow(hass, 1)
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "nothing_found"}


async def test_options_flow_events(hass: HomeAssistant, panel: FakePanel) -> None:
    result = await _start_flow(hass, panel.port)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PIN: "1234"})
    entry = result["result"]
    await hass.async_block_till_done()

    # A second fake service stands in for the events service on another port.
    events_panel = FakePanel()
    await events_panel.start()
    try:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.MENU
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "events"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_EVENTS_PORT: panel.port, CONF_EVENTS_PIN: "4321"}
        )
        assert result["errors"] == {CONF_EVENTS_PORT: "events_same_port"}
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_EVENTS_PORT: events_panel.port, CONF_EVENTS_PIN: "4321"}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert entry.options == {CONF_EVENTS_PORT: events_panel.port, CONF_EVENTS_PIN: "4321"}
        await hass.async_block_till_done()
        assert hass.states.get("event.protege_wx_panel_event") is not None
        await hass.config_entries.async_unload(entry.entry_id)
    finally:
        await events_panel.stop()


async def test_operator_is_optional_with_the_control_service(hass: HomeAssistant, panel: FakePanel) -> None:
    """A Protege GX has no WX web interface: setup goes ahead on the control service alone."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: 1, CONF_CONTROL_PORT: panel.port}
    )
    assert result["step_id"] == "acs"
    assert "No web operator" in result["description_placeholders"]["names"]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PIN: "1234"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].options == {}
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(result["result"].entry_id)


async def test_operator_is_required_without_the_control_service(hass: HomeAssistant, socket_enabled) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: 1, CONF_CONTROL_PORT: 1}
    )
    assert result["errors"] == {"base": "operator_required"}


async def test_options_flow_record_ids(hass: HomeAssistant, panel: FakePanel) -> None:
    panel.outputs[300] = ["CP001:09", 0]
    result = await _start_flow(hass, panel.port)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PIN: "1234"})
    entry = result["result"]
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "records"})
    assert result["step_id"] == "records"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"outputs": "1-2, x", CONF_DISCOVERY_GAP: 16}
    )
    assert result["errors"] == {"outputs": "invalid_ids"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"outputs": "300=Garage light", CONF_DISCOVERY_GAP: 16}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_DISCOVERY_GAP] == 16
    assert entry.options[CONF_PINNED] == [
        {"type": "output", "index": 300, "reference": None, "name": "Garage light"}
    ]
    await hass.async_block_till_done()
    # Saving reloads the entry, which reads the pinned output.
    assert hass.states.get("switch.garage_light") is not None

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "records"})
    schema = result["data_schema"].schema
    outputs = next(k for k in schema if k == "outputs")
    assert outputs.description == {"suggested_value": "300=Garage light"}
    await hass.config_entries.async_unload(entry.entry_id)
