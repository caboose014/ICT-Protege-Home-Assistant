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
    CONF_CONTROL_PORT,
    CONF_EVENTS_PIN,
    CONF_EVENTS_PORT,
    CONF_NUMBERING,
    CONF_PIN,
    CONF_RECORDS,
    CONF_USE_WEB,
    CONF_WX_PASSWORD,
    CONF_WX_PORT,
    CONF_WX_USERNAME,
    CONNECTION_ACS,
    CONNECTION_WEB,
    DEFAULT_PORT,
    DEFAULT_WX_PORT,
    DOMAIN,
)
from .hub import records_from_data, records_to_data
from .protege import CannotConnect, InvalidPin, ProtegeClient, ProtegeError, ProtegeEventClient, RecordType
from .protege.web import ProtegeWebClient, WebAuthError
from .protege.wx import WXAuthError, WXClient, WXError
from .upgrade import WrongPanel, async_upgrade, control_port, control_service_answers

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


async def _validate(
    data: dict[str, Any], listed: dict[RecordType, list[int]] | None = None
) -> tuple[str | None, str, list[dict[str, Any]], str]:
    """Connect, check the PIN, and discover records. Returns (serial, title, records, numbering).

    `listed` is the web interface's record IDs, so discovery can tell how the panel numbers records.
    """
    client = ProtegeClient(data[CONF_HOST], int(data[CONF_PORT]), data[CONF_PIN])
    info = await client.connect()
    try:
        await client.verify_pin()
        records = await client.discover(listed=listed)
    finally:
        await client.disconnect()
    title = f"Protege {info.firmware_type or 'WX'} ({data[CONF_HOST]})"
    return info.serial_str, title, records_to_data(records), client.numbering


def _user_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
            vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, "")): str,
            vol.Required(CONF_PASSWORD, default=defaults.get(CONF_PASSWORD, "")): PASSWORD_SELECTOR,
            vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_WX_PORT)): PORT_SELECTOR,
            vol.Required(
                CONF_CONTROL_PORT, default=defaults.get(CONF_CONTROL_PORT, DEFAULT_PORT)
            ): PORT_SELECTOR,
        }
    )


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


def upgrade_schema(defaults: dict[str, Any], port: int) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, port)): PORT_SELECTOR,
            vol.Required(CONF_PIN): PIN_SELECTOR,
        }
    )


def upgrade_errors(err: Exception) -> dict[str, str]:
    if isinstance(err, WrongPanel):
        return {"base": "wrong_panel"}
    return _errors_for(err)


class ProtegeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up an ICT Protege panel."""

    VERSION = 2

    def __init__(self) -> None:
        self._login: dict[str, Any] = {}
        self._web: tuple[str | None, str, list[dict[str, Any]]] | None = None
        self._web_error: Exception | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Address and web operator, then look for the Automation and Control service.

        The control service is used when it answers (it needs the Automation licence);
        otherwise everything goes through the web interface.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            login = {
                CONF_HOST: user_input[CONF_HOST].strip(),
                CONF_PORT: int(user_input[CONF_PORT]),
                CONF_USERNAME: user_input[CONF_USERNAME].strip(),
                CONF_PASSWORD: user_input[CONF_PASSWORD],
                CONF_CONTROL_PORT: int(user_input[CONF_CONTROL_PORT]),
            }
            self._async_abort_entries_match({CONF_HOST: login[CONF_HOST]})
            self._web, self._web_error = None, None
            try:
                self._web = await _validate_web(self.hass, login)
            except Exception as err:  # noqa: BLE001
                self._web_error = err
            # A wrong operator login is always worth fixing first.
            if isinstance(self._web_error, WebAuthError):
                errors = _errors_for(self._web_error)
            else:
                self._login = login
                if await control_service_answers(login[CONF_HOST], login[CONF_CONTROL_PORT]):
                    return await self.async_step_acs()
                if isinstance(self._web_error, CannotConnect):
                    errors = {"base": "nothing_found"}
                elif self._web_error is not None:
                    errors = _errors_for(self._web_error)
                else:
                    return await self.async_step_web()
        return self.async_show_form(step_id="user", data_schema=_user_schema(user_input or {}), errors=errors)

    async def async_step_acs(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The control service answered: ask for the integration PIN."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get(CONF_USE_WEB):
                if self._web is not None:
                    return await self.async_step_web()
                errors = _errors_for(self._web_error or CannotConnect())
            elif not user_input.get(CONF_PIN):
                errors[CONF_PIN] = "pin_required"
            else:
                data = {
                    CONF_HOST: self._login[CONF_HOST],
                    CONF_PORT: self._login[CONF_CONTROL_PORT],
                    CONF_PIN: str(user_input[CONF_PIN]),
                }
                listed: dict[RecordType, list[int]] | None = None
                if self._web is not None:
                    listed = {}
                    for r in records_from_data(self._web[2]):
                        listed.setdefault(r.record_type, []).append(r.index)
                try:
                    serial, title, records, numbering = await _validate(data, listed)
                except Exception as err:  # noqa: BLE001
                    errors = _errors_for(err)
                else:
                    if serial:
                        await self.async_set_unique_id(serial)
                        self._abort_if_unique_id_configured()
                    # The same operator reads names and zone settings (one login at start-up).
                    options = (
                        {
                            CONF_WX_USERNAME: self._login[CONF_USERNAME],
                            CONF_WX_PASSWORD: self._login[CONF_PASSWORD],
                            CONF_WX_PORT: self._login[CONF_PORT],
                        }
                        if self._web is not None
                        else {}
                    )
                    return self.async_create_entry(
                        title=title,
                        data={
                            **data,
                            CONF_CONNECTION: CONNECTION_ACS,
                            CONF_NUMBERING: numbering,
                            CONF_RECORDS: records,
                        },
                        options=options,
                    )
        schema = vol.Schema(
            {
                vol.Optional(CONF_PIN): PIN_SELECTOR,
                vol.Optional(CONF_USE_WEB, default=False): selector.BooleanSelector(),
            }
        )
        names = (
            "Names and zone settings will be read with the web operator."
            if self._web is not None
            else "The web interface could not be reached, so names come from module references "
            "until you set an operator under Configure."
        )
        return self.async_show_form(
            step_id="acs",
            data_schema=schema,
            errors=errors,
            description_placeholders={"port": str(self._login[CONF_CONTROL_PORT]), "names": names},
        )

    async def async_step_web(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Web interface only: choose how arming codes work."""
        assert self._web is not None
        if user_input is not None:
            serial, title, records = self._web
            if serial:
                await self.async_set_unique_id(serial)
                self._abort_if_unique_id_configured()
            # The control port is kept so the integration can offer the upgrade when it starts answering.
            data = dict(self._login)
            return self.async_create_entry(
                title=title,
                data={**data, CONF_CONNECTION: CONNECTION_WEB, CONF_RECORDS: records},
                options={CONF_CODE_MODE: user_input[CONF_CODE_MODE]},
            )
        schema = vol.Schema({vol.Required(CONF_CODE_MODE, default=CODE_MODE_HA): CODE_MODE_SELECTOR})
        return self.async_show_form(
            step_id="web",
            data_schema=schema,
            description_placeholders={"port": str(self._login[CONF_CONTROL_PORT])},
        )

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

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        web = entry.data.get(CONF_CONNECTION) == CONNECTION_WEB
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_PORT] = int(user_input[CONF_PORT])
            try:
                extra: dict[str, Any] = {}
                if web:
                    serial, _title, records = await _validate_web(self.hass, {**entry.data, **user_input})
                else:
                    listed: dict[RecordType, list[int]] = {}
                    for r in records_from_data(entry.data.get(CONF_RECORDS, [])):
                        listed.setdefault(r.record_type, []).append(r.index)
                    serial, _title, records, numbering = await _validate(user_input, listed or None)
                    extra[CONF_NUMBERING] = numbering
            except Exception as err:  # noqa: BLE001
                errors = _errors_for(err)
            else:
                if serial and entry.unique_id and serial != entry.unique_id:
                    return self.async_abort(reason="wrong_panel")
                return self.async_update_reload_and_abort(
                    entry, data_updates={**user_input, **extra, CONF_RECORDS: records}
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
            return self.async_show_menu(step_id="init", menu_options=["codes", "upgrade"])
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

    async def async_step_upgrade(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Move onto the Automation and Control service, keeping entities and their history."""
        entry = self.config_entry
        errors: dict[str, str] = {}
        if user_input is not None:
            port = int(user_input[CONF_PORT])
            try:
                await async_upgrade(self.hass, entry, port, str(user_input[CONF_PIN]))
            except Exception as err:  # noqa: BLE001
                errors = upgrade_errors(err)
            else:
                return self.async_abort(reason="upgraded")
        return self.async_show_form(
            step_id="upgrade",
            data_schema=upgrade_schema(user_input or {}, control_port(entry)),
            errors=errors,
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
                    CONF_NUMBERING,
                    description={"suggested_value": current.get(CONF_EVENTS_PIN)},
                ): PIN_SELECTOR,
            }
        )
        return self.async_show_form(step_id="events", data_schema=schema, errors=errors)
