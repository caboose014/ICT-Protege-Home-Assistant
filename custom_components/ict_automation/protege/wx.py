"""Read record names from the Protege WX web interface.

The ACS protocol carries no names, so they come from the controller's HTTP API
(``PRT_CTRL_DIN_ISAPI.dll``, see ICT's "Protege WX DLL API Reference"). Only the
HTTPS login for firmware 4.00.1676 or later is supported, and only List requests
are sent: this client never changes programming.

Login (per the reference and github.com/Hohaia/WXClient):
  1. ``Command&Type=Session&SubType=InitSession`` returns a random number R and a session cookie.
  2. ``Name`` = SHA-1(xor_hex(username, R + 1)), ``Password`` = SHA-1(xor_hex(sha1(password), R)).
  3. ``Command&Type=Session&SubType=CheckPasswordServer&Name=..&Password=..``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass

import aiohttp

from .models import Record, RecordType

_LOGGER = logging.getLogger(__name__)

PATH = "/PRT_CTRL_DIN_ISAPI.dll?"
TIMEOUT = aiohttp.ClientTimeout(total=15)

TABLE_INPUT_TYPES = "GXT_INPUTTYPES_TBL"
# Each input can sit in up to four areas, each with its own input type.
INPUT_AREA_SLOTS = ("1", "2", "3", "4")
NO_RECORD = (-1, 0xFFFFFFFF)

TABLES = {
    RecordType.DOOR: "GXT_DOORS_TBL",
    RecordType.AREA: "GXT_AREAS_TBL",
    RecordType.INPUT: "GXT_INPUTS_TBL",
    RecordType.OUTPUT: "GXT_PGMS_TBL",
    RecordType.TROUBLE_INPUT: "GXT_TROUBLEINPUTS_TBL",
}


class WXError(Exception):
    """The web interface could not be reached or refused a request."""


class WXAuthError(WXError):
    """The operator name or password was rejected."""


def xor_hex(text: str, number: int) -> str:
    """XOR each character with the next byte of `number` (lowest first, repeating), as upper hex."""
    key = number.to_bytes(4, "little", signed=False)
    return bytes(ord(c) & 0xFF ^ key[i % 4] for i, c in enumerate(text)).hex().upper()


def _sha1_upper(text: str) -> str:
    return hashlib.sha1(text.encode("latin-1"), usedforsecurity=False).hexdigest().upper()


def login_params(username: str, password: str, rand: int) -> tuple[str, str]:
    password_hash = hashlib.sha1(password.encode("utf-8"), usedforsecurity=False).hexdigest().lower()
    return (
        _sha1_upper(xor_hex(username, (rand + 1) & 0xFFFFFFFF)),
        _sha1_upper(xor_hex(password_hash, rand)),
    )


def _is_fail(text: str) -> bool:
    text = text.strip()
    return text.startswith(("FAIL", "Request Failed", "Command Failed"))


def _decode(value: str) -> str:
    # The controller swaps delimiters inside values: 0xE0 -> '&', 0xCD -> '='.
    return value.replace("\xe0", "&").replace("\xcd", "=")


def parse_list(text: str) -> list[tuple[int, str]]:
    """Parse a List reply ("recId=label&recId=label") into (id, label) sorted by id."""
    items: list[tuple[int, str]] = []
    for pair in text.strip().split("&"):
        if not pair or "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        try:
            items.append((int(key.strip()), _decode(value).strip()))
        except ValueError:
            continue
    return sorted(items)


def parse_detail(text: str) -> dict[str, str]:
    """Parse a Detail reply ("Field=value&Field=value") into a dict."""
    fields: dict[str, str] = {}
    for pair in text.strip().split("&"):
        if "=" in pair:
            key, value = pair.split("=", 1)
            fields[key.strip()] = _decode(value).strip()
    return fields


def _record_id(value: str | None) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return None if number in NO_RECORD or number < 0 else number


# Input type options that let an input stay open while its area arms.
# TODO(site): confirm the field name against a real GXT_INPUTTYPES_TBL detail reply.
_ARM_OPEN_FIELDS: tuple[str, ...] = ()


def allowed_open_when_arming(input_type: dict[str, str]) -> bool:
    """True when this input type does not need to be closed for its area to arm."""
    return any(input_type.get(name, "").lower() in ("true", "1", "yes") for name in _ARM_OPEN_FIELDS)


@dataclass
class WXSite:
    """Everything read from the web interface in one login."""

    lists: dict[RecordType, list[tuple[int, str]]]
    inputs: dict[int, dict[str, str]]  # WX input id -> detail fields
    input_types: dict[int, dict[str, str]]  # WX input type id -> detail fields


@dataclass
class WXClient:
    session: aiohttp.ClientSession
    host: str
    username: str
    password: str
    port: int = 443
    scheme: str = "https"  # tests only; the integration always uses HTTPS

    _cookie: str | None = None
    _logged_in: bool = False

    @property
    def _base(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}{PATH}"

    async def _post(self, params: str) -> str:
        headers = {"Cookie": self._cookie} if self._cookie else {}
        try:
            async with self.session.post(
                self._base + params, headers=headers, ssl=False, timeout=TIMEOUT
            ) as resp:
                if resp.status != 200:
                    raise WXError(f"HTTP {resp.status}")
                cookie = resp.headers.get("Set-Cookie")
                if cookie:
                    self._cookie = cookie.split(";", 1)[0].strip()
                body = await resp.read()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise WXError(f"Cannot reach the web interface: {err}") from err
        return body.decode("latin-1")

    async def login(self) -> None:
        reply = await self._post("Command&Type=Session&SubType=InitSession")
        if _is_fail(reply):
            raise WXError(
                f"The web interface refused a session ({reply.strip()[:60]}); firmware 4.00.1676 or later is needed"
            )
        try:
            rand = int(reply.strip())
        except ValueError as err:
            raise WXError("Unexpected session reply") from err
        name, password = login_params(self.username, self.password, rand)
        reply = await self._post(
            f"Command&Type=Session&SubType=CheckPasswordServer&Name={name}&Password={password}"
        )
        if _is_fail(reply):
            raise WXAuthError("The operator name or password was rejected")
        self._logged_in = True

    async def logout(self) -> None:
        if not self._logged_in:
            return
        self._logged_in = False
        try:
            await self._post("Command&Type=Session&SubType=CloseSession")
        except WXError as err:
            _LOGGER.debug("WX logout failed: %s", err)

    async def list(self, record_type: RecordType) -> list[tuple[int, str]]:
        reply = await self._post(f"Request&Type=List&SubType={TABLES[record_type]}")
        if _is_fail(reply):
            raise WXError(f"Listing {record_type.value}s failed: {reply.strip()[:60]}")
        return parse_list(reply)

    async def detail(self, table: str, rec_id: int) -> dict[str, str]:
        """Every field of one record, or an empty dict if the operator may not see it."""
        reply = await self._post(f"Request&Type=Detail&SubType={table}&RecId={rec_id}")
        if _is_fail(reply):
            return {}
        return parse_detail(reply)

    async def _lists(self) -> dict[RecordType, list[tuple[int, str]]]:
        result = {}
        for record_type in TABLES:
            result[record_type] = await self.list(record_type)
            await asyncio.sleep(0)
        return result

    async def fetch_all(self) -> dict[RecordType, list[tuple[int, str]]]:
        await self.login()
        try:
            return await self._lists()
        finally:
            await self.logout()

    async def fetch_site(self) -> WXSite:
        """Names, plus each input's areas and input types, in one login.

        This is programming, not live state, so it is read once (at start-up and on
        Rescan) and the session is closed straight away.
        """
        await self.login()
        try:
            lists = await self._lists()
            inputs: dict[int, dict[str, str]] = {}
            for rec_id, _ in lists.get(RecordType.INPUT, []):
                inputs[rec_id] = await self.detail(TABLES[RecordType.INPUT], rec_id)
            type_ids = {
                type_id
                for fields in inputs.values()
                for slot in INPUT_AREA_SLOTS
                if (type_id := _record_id(fields.get(f"ZoneType{slot}"))) is not None
            }
            input_types = {
                type_id: await self.detail(TABLE_INPUT_TYPES, type_id) for type_id in sorted(type_ids)
            }
            return WXSite(lists, inputs, input_types)
        finally:
            await self.logout()


def apply_names(records: list[Record], lists: dict[RecordType, list[tuple[int, str]]]) -> int:
    """Name records from WX lists. Returns how many records were named.

    WX record IDs are database IDs with gaps, not ACS indexes, but the lists come
    back in the same order as the ACS records (checked on site: the counts match
    and module references line up). So match by position, and only when the
    counts agree, to avoid naming the wrong record.
    """
    named = 0
    for record_type, items in lists.items():
        ours = sorted((r for r in records if r.record_type is record_type), key=lambda r: r.index)
        if not ours:
            continue
        if len(ours) != len(items):
            _LOGGER.warning(
                "Not naming %ss: the web interface lists %d but the panel reports %d",
                record_type.value,
                len(items),
                len(ours),
            )
            continue
        for record, (_, label) in zip(ours, items):
            if label:
                record.name = label
                named += 1
    return named


def apply_zone_settings(records: list[Record], site: WXSite) -> int:
    """Give inputs their areas, and the areas where they may be open when arming.

    WX ids map to ACS records by position, as for names. Returns how many inputs were set.
    """
    areas = sorted((r for r in records if r.record_type is RecordType.AREA), key=lambda r: r.index)
    inputs = sorted((r for r in records if r.record_type is RecordType.INPUT), key=lambda r: r.index)
    wx_areas = site.lists.get(RecordType.AREA, [])
    wx_inputs = site.lists.get(RecordType.INPUT, [])
    if len(areas) != len(wx_areas) or len(inputs) != len(wx_inputs):
        _LOGGER.warning("Not reading zone settings: the web interface and the panel list different records")
        return 0
    area_index = {wx_id: record.index for (wx_id, _), record in zip(wx_areas, areas)}
    updated = 0
    for record, (wx_id, _) in zip(inputs, wx_inputs):
        fields = site.inputs.get(wx_id)
        if not fields:
            continue  # the operator can't read this input's details
        record.areas, record.arm_open = [], []
        for slot in INPUT_AREA_SLOTS:
            index = area_index.get(_record_id(fields.get(f"Area{slot}")))
            if index is None or index in record.areas:
                continue
            record.areas.append(index)
            input_type = site.input_types.get(_record_id(fields.get(f"ZoneType{slot}")))
            if input_type and allowed_open_when_arming(input_type):
                record.arm_open.append(index)
        updated += 1
    return updated
