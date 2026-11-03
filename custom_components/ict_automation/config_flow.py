"""Config flow for ICT Protege."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_EVENTS_PIN,
    CONF_EVENTS_PORT,
    CONF_PIN,
    CONF_RECORDS,
    CONF_WX_PASSWORD,
    CONF_WX_PORT,
    CONF_WX_USERNAME,
    DEFAULT_PORT,
    DEFAULT_WX_PORT,
    DOMAIN,
)
from .hub import records_to_data
from .protege import CannotConnect, InvalidPin, ProtegeClient, ProtegeError, ProtegeEventClient, RecordType
from .protege.wx import WXAuthError, WXClient, WXError

_LOGGER = logging.getLogger(__name__)

PIN_SELECTOR = selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD))
PASSWORD_SELECTOR = PIN_SELECTOR
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
    """Names from the WX web interface, and optional event logging on a second service."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["names", "events"])

    def _save(self, changes: dict[str, Any]) -> ConfigFlowResult:
        options = {k: v for k, v in {**self.config_entry.options, **changes}.items() if v not in (None, "")}
        return self.async_create_entry(data=options)

    async def async_step_names(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            username = (user_input.get(CONF_WX_USERNAME) or "").strip()
            password = user_input.get(CONF_WX_PASSWORD) or ""
            port = int(user_input.get(CONF_WX_PORT) or DEFAULT_WX_PORT)
            if not username and not password:
                return self._save({CONF_WX_USERNAME: None, CONF_WX_PASSWORD: None, CONF_WX_PORT: None})
            if not username or not password:
                errors["base"] = "wx_incomplete"
            else:
                client = WXClient(
                    async_get_clientsession(self.hass, verify_ssl=False),
                    self.config_entry.data[CONF_HOST],
                    username,
                    password,
                    port,
                )
                try:
                    await client.login()
                    await client.list(RecordType.DOOR)
                except WXAuthError:
                    errors["base"] = "wx_invalid_auth"
                except WXError as err:
                    _LOGGER.debug("WX check failed: %s", err)
                    errors["base"] = "wx_cannot_connect"
                else:
                    # Saving reloads the entry, which reads the names.
                    return self._save(
                        {CONF_WX_USERNAME: username, CONF_WX_PASSWORD: password, CONF_WX_PORT: port}
                    )
                finally:
                    await client.logout()
        current = user_input or self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_WX_USERNAME, description={"suggested_value": current.get(CONF_WX_USERNAME)}
                ): str,
                vol.Optional(
                    CONF_WX_PASSWORD, description={"suggested_value": current.get(CONF_WX_PASSWORD)}
                ): PASSWORD_SELECTOR,
                vol.Optional(CONF_WX_PORT, default=current.get(CONF_WX_PORT, DEFAULT_WX_PORT)): PORT_SELECTOR,
            }
        )
        return self.async_show_form(step_id="names", data_schema=schema, errors=errors)

    async def async_step_events(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            port = user_input.get(CONF_EVENTS_PORT)
            pin = user_input.get(CONF_EVENTS_PIN)
            if not port and not pin:
                return self._save({CONF_EVENTS_PORT: None, CONF_EVENTS_PIN: None})
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
                    return self._save({CONF_EVENTS_PORT: int(port), CONF_EVENTS_PIN: str(pin)})
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
        return self.async_show_form(step_id="events", data_schema=schema, errors=errors)
