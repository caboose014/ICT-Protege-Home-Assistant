"""Web-only client: the WX web interface instead of the Automation and Control service.

For controllers without the Automation and Integration licence. It talks to the same
HTTPS API as the WX web pages (``PRT_CTRL_DIN_ISAPI.dll``, firmware 4.00.1676 or later,
where requests and replies are plain text inside TLS), and offers the same interface as
ProtegeClient so the Home Assistant layer doesn't care which one it has.

There is no push, so it polls, but gently:

* one session, kept for as long as the integration runs and logged out on unload;
* the event log's ``Update`` request (new events since the last read, an empty reply
  when nothing happened) is the change feed, read every couple of seconds;
* a status table is re-read only when an event touches it, after a command, and in a
  slow full refresh as a safety net;
* a session the controller drops is re-established, but logins are rate limited and
  back off, and a rejected password stops all further logins until it is fixed. A
  controller whose session table fills with abandoned sessions has been seen to stop
  answering, and failed logins count towards the operator's lockout.

Record indexes are the controller's own record IDs (so they have gaps).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections import deque
from collections.abc import Callable, Iterable

import aiohttp

from . import protocol as p
from .client import CannotConnect, CommandRejected, InvalidPin, ProtegeError
from .models import (
    AreaState,
    AreaStatus,
    DoorLockState,
    DoorState,
    DoorStatus,
    InputState,
    InputStatus,
    OutputState,
    OutputStatus,
    PanelEvent,
    PanelInfo,
    Record,
    RecordType,
    Status,
    StatusUpdate,
    event_user,
)
from .wx import (
    NO_RECORD,
    TABLE_INPUT_TYPES,
    TABLES,
    WXAuthError,
    WXClient,
    WXError,
    _is_fail,
    input_type_ids,
    parse_detail,
    parse_list,
    zone_settings,
)

_LOGGER = logging.getLogger(__name__)

# Status reply keys, e.g. "Input14=1,0".
_STATUS_PREFIX = {
    RecordType.DOOR: "Door",
    RecordType.AREA: "Area",
    RecordType.OUTPUT: "Output",
    RecordType.INPUT: "Input",
    RecordType.TROUBLE_INPUT: "TroubleInput",
}
_KEY = re.compile(r"^(\D+)(\d+)$")

# Control verbs (Command&Type=Control&SubType=<table>&RecId=<id>&Command=<n>), from ICT's
# "Protege WX DLL API Reference", Control topic.
WEB_AREA_COMMANDS = {
    p.AREA_DISARM: 0,
    p.AREA_DISARM_24H: 1,
    p.AREA_DISARM_ALL: 2,
    p.AREA_ARM_NORMAL: 3,
    p.AREA_ARM_FORCE: 4,
    p.AREA_ARM_STAY: 10,
    p.AREA_ARM_INSTANT: 5,
}
WEB_DOOR_LOCK, WEB_DOOR_UNLOCK, WEB_DOOR_UNLOCK_LATCHED = 0, 1, 2
WEB_INPUT_UNBYPASS, WEB_INPUT_BYPASS, WEB_INPUT_BYPASS_LATCHED = 0, 1, 2
WEB_OUTPUT_OFF, WEB_OUTPUT_ON, WEB_OUTPUT_ON_TIMED = 0, 1, 2

# Web door lock codes (Status 1) mapped onto the ACS ones the entities understand.
_DOOR_LOCK = {
    0: DoorLockState.LOCKED,
    1: DoorLockState.UNLOCKED_USER,
    2: DoorLockState.UNLOCKED_SCHEDULE,
    3: DoorLockState.UNLOCKED_USER_TIMED,
    4: DoorLockState.UNLOCKED_LATCHED,
    5: DoorLockState.UNLOCKED_REX,
    6: DoorLockState.UNLOCKED_REN,
    7: DoorLockState.UNLOCKED_USER,  # by operator
    8: DoorLockState.UNLOCKED_USER_TIMED,  # by operator, timed
    9: DoorLockState.UNLOCKED_LATCHED,  # by operator, latched
    10: DoorLockState.UNLOCKED_AREA,
    11: DoorLockState.UNLOCKED_FIRE,
    12: DoorLockState.LOCKED,  # by a calendar action
    13: DoorLockState.UNLOCKED_SCHEDULE,  # by a calendar action
    14: DoorLockState.UNLOCKED_USER_TIMED,  # extended door time
    15: DoorLockState.UNLOCKED_REX,
    16: DoorLockState.UNLOCKED_REN,
    17: DoorLockState.UNLOCKED_USER_TIMED,
    18: DoorLockState.LOCKED,
    19: DoorLockState.LOCKED,  # lockdown, entry allowed
    20: DoorLockState.LOCKED,
    21: DoorLockState.LOCKED,
    22: DoorLockState.LOCKED,
}
_TIMED_DOOR_CODES = frozenset({1, 3, 7, 8, 14, 15, 16, 17, 5, 6})

# Event categories (first word of a log line) -> tables whose status it can change.
_EVENT_TABLES = {
    "door": (RecordType.DOOR,),
    "user": (RecordType.DOOR, RecordType.AREA),
    "area": (RecordType.AREA, RecordType.INPUT),
    "input": (RecordType.INPUT, RecordType.AREA),
    "output": (RecordType.OUTPUT,),
    "trouble": (RecordType.TROUBLE_INPUT,),
}
_STAMP = re.compile(r"^(?:[A-Za-z]{3}\s+)?\S+\s+\d{1,2}:\d{2}:\d{2}\s+")
# "Front Door Reed - RE01.I01" -> module RE01, point I01.
_MODULE_SUFFIX = re.compile(r"\s+-\s+([A-Z]{2}\d{2,3})\.([A-Z]+\d+)\s*$")

SCHEME = "https"  # tests use plain HTTP
_INVALID_SESSION = "<invalid session>"
_NOTHING = ("", "<NO RESPONSE>")

LOGIN_WINDOW = 3600.0
MAX_LOGINS_PER_WINDOW = 6
BACKOFF_START = 30.0
BACKOFF_MAX = 600.0

StatusListener = Callable[[StatusUpdate], None]
ConnectionListener = Callable[[bool], None]
EventListener = Callable[[PanelEvent], None]
CookieListener = Callable[[str | None], None]


class WebAuthError(InvalidPin):
    """The operator name or password was rejected. No further logins are tried."""

    def __init__(self, message: str = "The web interface rejected the operator name or password") -> None:
        ProtegeError.__init__(self, message)
        self.code = None


def _ints(csv: str, count: int) -> list[int | None]:
    values: list[int | None] = []
    for part in csv.split(","):
        try:
            values.append(int(part.strip()))
        except ValueError:
            values.append(None)
    return values + [None] * (count - len(values))


def _enum(enum_cls, value: int | None, default: int = 0):
    if value is None:
        return default
    try:
        return enum_cls(value)
    except ValueError:
        return value


def parse_status(record_type: RecordType, reply: str, references: dict[int, str | None]) -> dict[int, Status]:
    """Decode a status reply ("Input0=1,0&Input1=0,0") into our status objects."""
    prefix = _STATUS_PREFIX[record_type]
    out: dict[int, Status] = {}
    for pair in reply.strip().split("&"):
        if "=" not in pair:
            continue
        key, csv = pair.split("=", 1)
        match = _KEY.match(key.strip())
        if not match or match.group(1) != prefix:
            continue
        rec_id = int(match.group(2))
        if rec_id in NO_RECORD:
            continue
        if record_type is RecordType.DOOR:
            lock, position = _ints(csv, 2)[:2]
            lock_state = _DOOR_LOCK.get(lock, lock if lock is not None else 0)
            out[rec_id] = DoorStatus(lock_state, _enum(DoorState, position))
        elif record_type is RecordType.AREA:
            area_24h, state, flags = _ints(csv, 3)[:3]
            out[rec_id] = AreaStatus(_enum(AreaState, state), area_24h or 0, flags or 0)
        elif record_type is RecordType.OUTPUT:
            (state,) = _ints(csv, 1)[:1]
            out[rec_id] = OutputStatus(references.get(rec_id) or "", _enum(OutputState, state))
        else:
            state, flags = _ints(csv, 2)[:2]
            # The web API has 2 = tamper and 3 = short, the other way round from ACS.
            state = {2: InputState.TAMPER, 3: InputState.SHORT}.get(state, state)
            flags = flags or 0
            # Siren lockout is bit 2 here and bit 3 over ACS.
            acs_flags = (flags & 0x03) | (0x08 if flags & 0x04 else 0)
            out[rec_id] = InputStatus(references.get(rec_id) or "", _enum(InputState, state), acs_flags)
    return out


def parse_events(reply: str) -> list[PanelEvent]:
    """Decode an Events reply ("Event0=<line>&EventCodes0=..."), oldest first."""
    lines: list[tuple[int, str]] = []
    for pair in reply.strip().split("&"):
        if "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        if key.startswith("Event") and key[5:].isdigit() and value.strip():
            lines.append((int(key[5:]), value.strip()))
    events = []
    # The controller lists the newest first.
    for _, line in sorted(lines, reverse=True):
        text = re.sub(r"\s*\[Card [^\]]*\]", "", _STAMP.sub("", line)).strip()
        category, _, description = text.partition(" ")
        event = PanelEvent(text=text, category=category or None, description=description or None)
        event.user = event_user(event)
        events.append(event)
    return events


def record_from_label(record_type: RecordType, rec_id: int, label: str) -> Record:
    """A record from a List entry, with the module reference its label carries."""
    match = _MODULE_SUFFIX.search(label)
    reference = f"{match.group(1)}:{match.group(2)}" if match else None
    return Record(record_type, rec_id, reference, label or None)


class ProtegeWebClient:
    """Same interface as ProtegeClient, over the WX web interface."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        username: str,
        password: str,
        port: int = 443,
        *,
        scheme: str | None = None,
        event_interval: float = 2.0,
        full_refresh_interval: float = 30.0,
        retry_interval: float = 15.0,
    ) -> None:
        self.host = host
        self.port = port
        self._wx = WXClient(session, host, username, password, port, scheme=scheme or SCHEME)
        self.event_interval = event_interval
        self.full_refresh_interval = full_refresh_interval
        self.retry_interval = retry_interval
        self.panel = PanelInfo()
        self.records: dict[tuple[RecordType, int], Record] = {}
        self.status: dict[tuple[RecordType, int], Status] = {}
        self._status_listeners: list[StatusListener] = []
        self._connection_listeners: list[ConnectionListener] = []
        self._event_listeners: list[EventListener] = []
        self._cookie_listeners: list[CookieListener] = []
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._background: set[asyncio.Task] = set()
        self._wake = asyncio.Event()
        self._dirty: set[RecordType] = set()
        self._available = False
        self._stopping = False
        self._auth_error: WebAuthError | None = None
        self._login_times: deque[float] = deque()
        self._backoff = 0.0
        self._backoff_until = 0.0
        self._events_primed = False

    # ----- listeners -----------------------------------------------------

    @property
    def available(self) -> bool:
        return self._available

    @property
    def auth_failed(self) -> bool:
        return self._auth_error is not None

    def add_status_listener(self, listener: StatusListener) -> Callable[[], None]:
        self._status_listeners.append(listener)
        return lambda: self._status_listeners.remove(listener)

    def add_connection_listener(self, listener: ConnectionListener) -> Callable[[], None]:
        self._connection_listeners.append(listener)
        return lambda: self._connection_listeners.remove(listener)

    def add_listener(self, listener: EventListener) -> Callable[[], None]:
        """Panel events, as the events client offers them."""
        self._event_listeners.append(listener)
        return lambda: self._event_listeners.remove(listener)

    def add_cookie_listener(self, listener: CookieListener) -> Callable[[], None]:
        """Called with the session cookie after each login (None after logout), to save it."""
        self._cookie_listeners.append(listener)
        return lambda: self._cookie_listeners.remove(listener)

    def _set_available(self, available: bool) -> None:
        if available == self._available:
            return
        self._available = available
        for listener in list(self._connection_listeners):
            listener(available)

    # ----- session --------------------------------------------------------

    def resume(self, cookie: str | None) -> None:
        """Try a session saved before a restart before logging in again."""
        if cookie:
            self._wx.resume(cookie)

    def _check_login_allowed(self) -> None:
        if self._auth_error is not None:
            raise self._auth_error
        now = time.monotonic()
        if now < self._backoff_until:
            raise CannotConnect(f"Waiting {int(self._backoff_until - now)} s before logging in again")
        while self._login_times and now - self._login_times[0] > LOGIN_WINDOW:
            self._login_times.popleft()
        if len(self._login_times) >= MAX_LOGINS_PER_WINDOW:
            self._start_backoff("too many logins in an hour")
            raise CannotConnect("Too many logins; backing off to protect the controller")
        self._login_times.append(now)

    def _start_backoff(self, reason: str) -> None:
        self._backoff = min(BACKOFF_MAX, self._backoff * 2 if self._backoff else BACKOFF_START)
        self._backoff_until = time.monotonic() + self._backoff
        _LOGGER.warning("WX web interface at %s: %s; waiting %d s", self.host, reason, self._backoff)

    async def _login(self) -> None:
        self._check_login_allowed()
        try:
            await self._wx.login()
        except WXAuthError as err:
            self._auth_error = WebAuthError()
            raise self._auth_error from err
        except WXError as err:
            self._start_backoff(str(err))
            raise CannotConnect(str(err)) from err
        self._backoff = 0.0
        self._events_primed = False
        for listener in list(self._cookie_listeners):
            listener(self._wx.cookie)

    async def _request(self, params: str) -> str:
        """One request; logs in again once if the controller has dropped the session."""
        for attempt in range(2):
            if not self._wx.logged_in:
                await self._login()
            try:
                reply = await self._wx.request(params)
            except WXError as err:
                raise CannotConnect(str(err)) from err
            if reply.strip().lower().startswith(_INVALID_SESSION) or (
                reply.strip() == "FAIL" and params.startswith("Request&Type=Events")
            ):
                _LOGGER.debug("WX session ended (%s); logging in again", reply.strip()[:30])
                self._wx.forget_session()
                if attempt:
                    self._start_backoff("a new session was rejected straight away")
                    raise CannotConnect("The controller keeps rejecting the session")
                continue
            return reply
        raise CannotConnect("No session")

    # ----- connection ----------------------------------------------------

    async def connect(self) -> PanelInfo:
        """Log in (or pick up a saved session) and read the controller's details."""
        async with self._lock:
            reply = await self._request("Request&Type=Health&SubType=Values")
        fields = parse_detail(reply) if not _is_fail(reply) else {}
        serial = (fields.get("SerialNumber") or "").strip()
        self.panel.firmware_type = "WX"
        with contextlib.suppress(ValueError):
            self.panel.serial = int(serial, 16)
        version = re.match(r"(\d+)\.(\d+)", fields.get("WXVersion", ""))
        if version:
            self.panel.firmware_version = int(version.group(1)) * 100 + int(version.group(2))
        build = (fields.get("MainAppBuild") or "").strip()
        if build.isdigit():
            self.panel.firmware_build = int(build)
        return self.panel

    async def verify_pin(self) -> None:
        """The login itself checks the operator; nothing else to verify."""

    async def disconnect(self) -> None:
        await self._logout()

    async def _logout(self) -> None:
        async with self._lock:
            with contextlib.suppress(WXError):
                await self._wx.logout()
        for listener in list(self._cookie_listeners):
            listener(None)

    async def discover(self) -> list[Record]:
        """Every area, door, input, output and trouble input, named, with inputs' areas."""
        found: list[Record] = []
        async with self._lock:
            lists: dict[RecordType, list[tuple[int, str]]] = {}
            for record_type, table in TABLES.items():
                reply = await self._request(f"Request&Type=List&SubType={table}")
                lists[record_type] = [] if _is_fail(reply) else parse_list(reply)
            inputs: dict[int, dict[str, str]] = {}
            for rec_id, _ in lists[RecordType.INPUT]:
                reply = await self._request(
                    f"Request&Type=Detail&SubType={TABLES[RecordType.INPUT]}&RecId={rec_id}"
                )
                inputs[rec_id] = {} if _is_fail(reply) else parse_detail(reply)
            input_types = {}
            for type_id in input_type_ids(inputs):
                reply = await self._request(
                    f"Request&Type=Detail&SubType={TABLE_INPUT_TYPES}&RecId={type_id}"
                )
                input_types[type_id] = {} if _is_fail(reply) else parse_detail(reply)
        areas = {rec_id: rec_id for rec_id, _ in lists[RecordType.AREA]}
        for record_type in TABLES:
            for rec_id, label in lists[record_type]:
                if rec_id in NO_RECORD:
                    continue
                record = record_from_label(record_type, rec_id, label)
                if record_type is RecordType.INPUT and inputs.get(rec_id):
                    record.areas, record.arm_open = zone_settings(inputs[rec_id], input_types, areas)
                found.append(record)
        return found

    def set_records(self, records: Iterable[Record]) -> None:
        self.records = {r.key: r for r in records}

    async def start(self, records: Iterable[Record]) -> None:
        self.set_records(records)
        self._stopping = False
        self._dirty = set(_STATUS_PREFIX)
        # Read everything once now, so the entities start with real states.
        try:
            await self._poll_events()
            await self._read_dirty()
            self._set_available(True)
        except ProtegeError as err:  # the poll loop retries, or reports a rejected operator
            _LOGGER.debug("First WX read failed: %s", err)
            self._dirty = set(_STATUS_PREFIX)
        self._task = asyncio.create_task(self._run(time.monotonic() if self._available else 0.0))

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        tasks = [t for t in (self._task, *self._background) if t is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._task = None
        await self._logout()
        self._set_available(False)

    # ----- polling --------------------------------------------------------

    async def _run(self, last_full: float = 0.0) -> None:
        while not self._stopping:
            try:
                if time.monotonic() - last_full >= self.full_refresh_interval:
                    self._dirty = set(_STATUS_PREFIX)
                    last_full = time.monotonic()
                await self._poll_events()
                await self._read_dirty()
                self._set_available(True)
                delay = self.event_interval
            except WebAuthError:
                _LOGGER.error(
                    "The WX web interface rejected the operator; not logging in again until it is fixed"
                )
                self._set_available(False)
                return
            except ProtegeError as err:
                _LOGGER.debug("WX poll failed: %s", err)
                self._set_available(False)
                self._dirty = set(_STATUS_PREFIX)
                delay = max(self.retry_interval, self._backoff_until - time.monotonic())
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), delay)

    async def _poll_events(self) -> None:
        async with self._lock:
            if not self._events_primed:
                # Latest moves the session's cursor to now; those events already happened.
                await self._request("Request&Type=Events&SubType=Latest")
                self._events_primed = True
                return
            reply = await self._request("Request&Type=Events&SubType=Update")
        if reply.strip().upper() in _NOTHING or _is_fail(reply):
            return
        for event in parse_events(reply):
            self._dirty.update(_EVENT_TABLES.get((event.category or "").lower(), ()))
            for listener in list(self._event_listeners):
                listener(event)

    async def _read_dirty(self) -> None:
        tables, self._dirty = self._dirty, set()
        for record_type in _STATUS_PREFIX:
            if record_type in tables:
                await self.refresh(record_type)

    async def refresh(self, record_type: RecordType) -> None:
        """Read one status table and pass on what changed."""
        if not any(key[0] is record_type for key in self.records):
            return
        async with self._lock:
            reply = await self._request(f"Request&Type=Status&SubType={TABLES[record_type]}")
        if _is_fail(reply) or reply.strip().upper() in _NOTHING:
            return
        references = {index: r.reference for (rt, index), r in self.records.items() if rt is record_type}
        timed_door = False
        for index, status in parse_status(record_type, reply, references).items():
            key = (record_type, index)
            if key not in self.records:
                continue
            if isinstance(status, DoorStatus) and status.lock_state != DoorLockState.LOCKED:
                timed_door = timed_door or status.lock_state in (
                    DoorLockState.UNLOCKED_USER,
                    DoorLockState.UNLOCKED_USER_TIMED,
                    DoorLockState.UNLOCKED_REX,
                    DoorLockState.UNLOCKED_REN,
                )
            if self.status.get(key) == status:
                continue
            self._apply(StatusUpdate(record_type, index, status))
        if timed_door:
            # Timed unlocks relock on their own without an event; look again shortly.
            self._refresh_later(record_type, (3.0, 8.0))

    def _apply(self, update: StatusUpdate) -> None:
        self.status[(update.record_type, update.index)] = update.status
        for listener in list(self._status_listeners):
            listener(update)

    def _refresh_later(self, record_type: RecordType, delays: tuple[float, ...]) -> None:
        async def later() -> None:
            elapsed = 0.0
            for delay in delays:
                await asyncio.sleep(delay - elapsed)
                elapsed = delay
                if self._stopping:
                    return
                with contextlib.suppress(ProtegeError):
                    await self.refresh(record_type)

        task = asyncio.create_task(later())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def request_status(self, record_type: RecordType, index: int) -> Status | None:
        await self.refresh(record_type)
        return self.status.get((record_type, index))

    async def refresh_all(self) -> None:
        for record_type in _STATUS_PREFIX:
            await self.refresh(record_type)

    def watch_doors(self) -> None:
        self._dirty.add(RecordType.DOOR)
        self._wake.set()

    # ----- commands -------------------------------------------------------

    async def _control(
        self,
        record_type: RecordType,
        index: int,
        command: int,
        data1: int | None = None,
        *,
        assumed: Status | None = None,
    ) -> None:
        params = f"Command&Type=Control&SubType={TABLES[record_type]}&RecId={index}&Command={command}"
        if data1 is not None:
            params += f"&Data1={data1}"
        async with self._lock:
            reply = await self._request(params)
        if reply.strip().upper() != "OK":
            _LOGGER.debug("WX control %s refused: %r", params, reply[:80])
            raise CommandRejected(None)
        if assumed is not None:
            self._apply(StatusUpdate(record_type, index, assumed))
        await self.refresh(record_type)
        self._refresh_later(record_type, (1.0, 3.0, 6.0))

    def _assumed_door(self, index: int, lock_state: DoorLockState) -> DoorStatus:
        current = self.status.get((RecordType.DOOR, index))
        return DoorStatus(lock_state, current.door_state if isinstance(current, DoorStatus) else 0)

    async def door_lock(self, index: int) -> None:
        await self._control(
            RecordType.DOOR, index, WEB_DOOR_LOCK, assumed=self._assumed_door(index, DoorLockState.LOCKED)
        )

    async def door_unlock(self, index: int) -> None:
        await self._control(
            RecordType.DOOR,
            index,
            WEB_DOOR_UNLOCK,
            assumed=self._assumed_door(index, DoorLockState.UNLOCKED_USER),
        )
        self._refresh_later(RecordType.DOOR, (10.0, 20.0, 35.0, 60.0))

    async def door_unlock_latched(self, index: int) -> None:
        await self._control(
            RecordType.DOOR,
            index,
            WEB_DOOR_UNLOCK_LATCHED,
            assumed=self._assumed_door(index, DoorLockState.UNLOCKED_LATCHED),
        )

    async def area_command(self, index: int, sub_command: int, code: str | None = None) -> None:
        """Arm or disarm. The web interface acts as the operator; any code is checked by the caller."""
        try:
            command = WEB_AREA_COMMANDS[sub_command]
        except KeyError as err:
            raise CommandRejected(p.NACK_SERVICE_COMMAND_NOT_VALID) from err
        await self._control(RecordType.AREA, index, command)

    async def output_on(self, index: int, seconds: int | None = None) -> None:
        current = self.status.get((RecordType.OUTPUT, index))
        reference = current.reference if isinstance(current, OutputStatus) else ""
        if seconds:
            await self._control(
                RecordType.OUTPUT,
                index,
                WEB_OUTPUT_ON_TIMED,
                max(1, min(int(seconds), 65535)),
                assumed=OutputStatus(reference, OutputState.ON_TIMED),
            )
        else:
            await self._control(
                RecordType.OUTPUT, index, WEB_OUTPUT_ON, assumed=OutputStatus(reference, OutputState.ON)
            )

    async def output_off(self, index: int) -> None:
        current = self.status.get((RecordType.OUTPUT, index))
        reference = current.reference if isinstance(current, OutputStatus) else ""
        await self._control(
            RecordType.OUTPUT, index, WEB_OUTPUT_OFF, assumed=OutputStatus(reference, OutputState.OFF)
        )

    async def set_bypass(
        self, record_type: RecordType, index: int, bypass: bool, *, permanent: bool = False
    ) -> None:
        if record_type not in (RecordType.INPUT, RecordType.TROUBLE_INPUT):
            raise ValueError("Only inputs and trouble inputs can be bypassed")
        command = (
            (WEB_INPUT_BYPASS_LATCHED if permanent else WEB_INPUT_BYPASS) if bypass else WEB_INPUT_UNBYPASS
        )
        assumed = None
        current = self.status.get((record_type, index))
        if isinstance(current, InputStatus):
            flags = current.bypass_flags & ~0x03
            if bypass:
                flags |= 0x03 if permanent else 0x01
            assumed = InputStatus(current.reference, current.state, flags)
        await self._control(record_type, index, command, assumed=assumed)
