"""Config and options flow tests."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.ict_automation.const import (
    CONF_CONTROL_PORT,
    CONF_EVENTS_PIN,
    CONF_EVENTS_PORT,
    CONF_PIN,
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
