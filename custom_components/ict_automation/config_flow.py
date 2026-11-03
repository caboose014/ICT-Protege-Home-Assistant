"""Config flow for ICT Protege."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .codes import make_code
from .const import (
    CODE_MODE_HA,
    CODE_MODE_NONE,
    CONF_CODE_MODE,
    CONF_CODES,
    CONF_CONNECTION,
    CONF_EVENTS_PIN,
    CONF_EVENTS_PORT,
    CONF_PIN,
    CONF_RECORDS,
    CONF_WX_PASSWORD,
    CONF_WX_PORT,
    CONF_WX_USERNAME,
    CONNECTION_ACS,
    CONNECTION_WEB,
    DEFAULT_PORT,
    DEFAULT_WX_PORT,
    DOMAIN,
)
from .hub import records_to_data
from .protege import CannotConnect, InvalidPin, ProtegeClient, ProtegeError, ProtegeEventClient, RecordType
from .protege.web import ProtegeWebClient, WebAuthError
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


CODE_MODE_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=[CODE_MODE_HA, CODE_MODE_NONE],
        translation_key="code_mode",
        mode=selector.SelectSelectorMode.LIST,
    )
)


def _web_schema(defaults: dict[str, Any], *, with_codes: bool = True) -> vol.Schema:
    fields: dict[Any, Any] = {
        vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
        vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_WX_PORT)): PORT_SELECTOR,
        vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, "")): str,
        vol.Required(CONF_PASSWORD, default=defaults.get(CONF_PASSWORD, "")): PASSWORD_SELECTOR,
    }
    if with_codes:
        fields[vol.Required(CONF_CODE_MODE, default=defaults.get(CONF_CODE_MODE, CODE_MODE_HA))] = (
            CODE_MODE_SELECTOR
        )
    return vol.Schema(fields)


async def _validate_web(hass, data: dict[str, Any]) -> tuple[str | None, str, list[dict[str, Any]]]:
    """Log in to the web interface and discover records. Returns (serial, title, records)."""
    client = ProtegeWebClient(
        async_get_clientsession(hass, verify_ssl=False),
        data[CONF_HOST],
        data[CONF_USERNAME],
        data[CONF_PASSWORD],
        int(data[CONF_PORT]),
    )
    try:
        info = await client.connect()
        records = await client.discover()
    finally:
        await client.disconnect()
    return info.serial_str, f"Protege WX web ({data[CONF_HOST]})", records_to_data(records)


def _errors_for(err: Exception) -> dict[str, str]:
    if isinstance(err, WebAuthError):
        return {"base": "web_invalid_auth"}
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
        """ACS when the controller has the licence (recommended), else the web interface."""
        return self.async_show_menu(step_id="user", menu_options=["acs", "web"])

    async def async_step_web(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_PORT] = int(user_input[CONF_PORT])
            user_input[CONF_USERNAME] = user_input[CONF_USERNAME].strip()
            self._async_abort_entries_match({CONF_HOST: user_input[CONF_HOST]})
            try:
                serial, title, records = await _validate_web(self.hass, user_input)
            except Exception as err:  # noqa: BLE001
                errors = _errors_for(err)
            else:
                if serial:
                    await self.async_set_unique_id(serial)
                    self._abort_if_unique_id_configured()
                code_mode = user_input.pop(CONF_CODE_MODE)
                return self.async_create_entry(
                    title=title,
                    data={**user_input, CONF_CONNECTION: CONNECTION_WEB, CONF_RECORDS: records},
                    options={CONF_CODE_MODE: code_mode},
                )
        return self.async_show_form(step_id="web", data_schema=_web_schema(user_input or {}), errors=errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The web interface rejected the operator: ask for the name and password again."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, **user_input, CONF_USERNAME: user_input[CONF_USERNAME].strip()}
            try:
                await _validate_web(self.hass, data)
            except Exception as err:  # noqa: BLE001
                errors = _errors_for(err)
            else:
                return self.async_update_reload_and_abort(entry, data_updates=data)
        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME, default=entry.data.get(CONF_USERNAME, "")): str,
                vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
            }
        )
        return self.async_show_form(step_id="reauth_confirm", data_schema=schema, errors=errors)

    async def async_step_acs(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
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
                return self.async_create_entry(
                    title=title, data={**user_input, CONF_CONNECTION: CONNECTION_ACS, CONF_RECORDS: records}
                )
        return self.async_show_form(step_id="acs", data_schema=_schema(user_input or {}), errors=errors)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        web = entry.data.get(CONF_CONNECTION) == CONNECTION_WEB
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_PORT] = int(user_input[CONF_PORT])
            try:
                if web:
                    serial, _title, records = await _validate_web(self.hass, {**entry.data, **user_input})
                else:
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
            step_id="reconfigure_web" if web else "reconfigure",
            data_schema=_web_schema(user_input or dict(entry.data), with_codes=False)
            if web
            else _schema(user_input or dict(entry.data)),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return ProtegeOptionsFlow()


class ProtegeOptionsFlow(OptionsFlow):
    """Names from the WX web interface, and optional event logging on a second service."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self.config_entry.data.get(CONF_CONNECTION) == CONNECTION_WEB:
            # Names and events already come from the web interface.
            return await self.async_step_codes()
        return self.async_show_menu(step_id="init", menu_options=["names", "events"])

    async def async_step_codes(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Web-only mode: whether arming needs a code, and whose codes are accepted."""
        codes: list[dict[str, Any]] = list(self.config_entry.options.get(CONF_CODES, []))
        names = [c["name"] for c in codes]
        errors: dict[str, str] = {}
        if user_input is not None:
            remove = set(user_input.get("remove") or [])
            codes = [c for c in codes if c["name"] not in remove]
            name = (user_input.get("add_name") or "").strip()
            code = (user_input.get("add_code") or "").strip()
            if name or code:
                if not name or not code:
                    errors["base"] = "code_incomplete"
                elif not code.isdigit() or len(code) < 4:
                    errors["add_code"] = "code_too_short"
                elif name in [c["name"] for c in codes]:
                    errors["add_name"] = "code_name_taken"
                else:
                    codes.append(await self.hass.async_add_executor_job(make_code, name, code))
            if not errors:
                return self._save({CONF_CODE_MODE: user_input[CONF_CODE_MODE], CONF_CODES: codes or None})
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_CODE_MODE, default=self.config_entry.options.get(CONF_CODE_MODE, CODE_MODE_HA)
                ): CODE_MODE_SELECTOR,
                vol.Optional("add_name"): str,
                vol.Optional("add_code"): PIN_SELECTOR,
                vol.Optional("remove"): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=names, multiple=True)
                ),
            }
        )
        return self.async_show_form(
            step_id="codes",
            data_schema=schema,
            errors=errors,
            description_placeholders={"people": ", ".join(names) or "none yet"},
        )

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
