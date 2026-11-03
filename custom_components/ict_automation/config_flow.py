"""Config flow for ICT Protege."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_EVENTS_PIN,
    CONF_EVENTS_PORT,
    CONF_PIN,
    CONF_RECORDS,
    DEFAULT_PORT,
    DOMAIN,
)
from .hub import records_to_data
from .protege import CannotConnect, InvalidPin, ProtegeClient, ProtegeError, ProtegeEventClient

_LOGGER = logging.getLogger(__name__)

PIN_SELECTOR = selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD))
PORT_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(min=1, max=65535, mode=selector.NumberSelectorMode.BOX)
)


def _schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
            vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)): PORT_SELECTOR,
            vol.Required(CONF_PIN, default=defaults.get(CONF_PIN, "")): PIN_SELECTOR,
        }
    )


async def _validate(data: dict[str, Any]) -> tuple[str | None, str, list[dict[str, Any]]]:
    """Connect, check the PIN, and discover records. Returns (serial, title, records)."""
    client = ProtegeClient(data[CONF_HOST], int(data[CONF_PORT]), data[CONF_PIN])
    info = await client.connect()
    try:
        await client.verify_pin()
        records = await client.discover()
    finally:
        await client.disconnect()
    title = f"Protege {info.firmware_type or 'WX'} ({data[CONF_HOST]})"
    return info.serial_str, title, records_to_data(records)


def _errors_for(err: Exception) -> dict[str, str]:
    if isinstance(err, InvalidPin):
        return {CONF_PIN: "invalid_pin"}
    if isinstance(err, CannotConnect):
        return {"base": "cannot_connect"}
    if isinstance(err, ProtegeError):
        return {"base": "panel_error"}
    _LOGGER.exception("Unexpected error validating ICT panel")
    return {"base": "unknown"}


class ProtegeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up an ICT Protege panel."""

    VERSION = 2

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_PORT] = int(user_input[CONF_PORT])
            self._async_abort_entries_match({CONF_HOST: user_input[CONF_HOST]})
            try:
                serial, title, records = await _validate(user_input)
            except Exception as err:  # noqa: BLE001
                errors = _errors_for(err)
            else:
                if serial:
                    await self.async_set_unique_id(serial)
                    self._abort_if_unique_id_configured()
                return self.async_create_entry(title=title, data={**user_input, CONF_RECORDS: records})
        return self.async_show_form(step_id="user", data_schema=_schema(user_input or {}), errors=errors)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_PORT] = int(user_input[CONF_PORT])
            try:
                serial, _title, records = await _validate(user_input)
            except Exception as err:  # noqa: BLE001
                errors = _errors_for(err)
            else:
                if serial and entry.unique_id and serial != entry.unique_id:
                    return self.async_abort(reason="wrong_panel")
                return self.async_update_reload_and_abort(
                    entry, data_updates={**user_input, CONF_RECORDS: records}
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_schema(user_input or dict(entry.data)),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return ProtegeOptionsFlow()


class ProtegeOptionsFlow(OptionsFlow):
    """Optional event logging through a second, view-only ACS service."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            port = user_input.get(CONF_EVENTS_PORT)
            pin = user_input.get(CONF_EVENTS_PIN)
            if not port and not pin:
                return self.async_create_entry(data={})
            if not port or not pin:
                errors["base"] = "events_incomplete"
            elif int(port) == self.config_entry.data[CONF_PORT]:
                errors[CONF_EVENTS_PORT] = "events_same_port"
            else:
                client = ProtegeEventClient(self.config_entry.data[CONF_HOST], int(port), str(pin))
                try:
                    await client.open()
                except Exception as err:  # noqa: BLE001
                    errors = _errors_for(err)
                    if CONF_PIN in errors:
                        errors = {CONF_EVENTS_PIN: "invalid_pin"}
                else:
                    await client.stop()
                    return self.async_create_entry(
                        data={CONF_EVENTS_PORT: int(port), CONF_EVENTS_PIN: str(pin)}
                    )
        current = user_input or self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_EVENTS_PORT,
                    description={"suggested_value": current.get(CONF_EVENTS_PORT)},
                ): PORT_SELECTOR,
                vol.Optional(
                    CONF_EVENTS_PIN,
                    description={"suggested_value": current.get(CONF_EVENTS_PIN)},
                ): PIN_SELECTOR,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
