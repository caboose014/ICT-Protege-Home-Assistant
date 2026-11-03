"""A small fake of the WX web interface's HTTPS API (PRT_CTRL_DIN_ISAPI.dll), for web-only mode.

Reply formats follow what a WX 4.00 controller (web UI 2.3.628) sent on site.
"""

from __future__ import annotations

from aiohttp import web

from custom_components.ict_automation.protege.wx import login_params

RAND = 0x1234ABCD
USERNAME, PASSWORD = "homeassistant", "secret"
NOT_SET = 2147483647


class FakeWebController:
    def __init__(self) -> None:
        self.requests: list[str] = []
        self.sessions: set[str] = set()
        self._next_session = 0
        self.logins = 0
        self.failed_logins = 0
        self.lists = {
            "GXT_AREAS_TBL": [(0, "System Area"), (16, "Soundline")],
            "GXT_DOORS_TBL": [(0, "Front Door"), (1, "Jail Gate")],
            "GXT_INPUTS_TBL": [(0, "Front Door Reed - RE01.I01"), (14, "Gallery PIR - IE01.I01")],
            "GXT_PGMS_TBL": [(112, "Controller Bell 0")],
            "GXT_TROUBLEINPUTS_TBL": [(191, "12V Supply Failure")],
        }
        self.details = {
            ("GXT_INPUTS_TBL", 0): f"InputId=0&Name=Front Door Reed&Area1=16&ZoneType1=42&Area2={NOT_SET}",
            ("GXT_INPUTS_TBL", 14): f"InputId=14&Name=Gallery PIR&Area1=16&ZoneType1=50&Area2={NOT_SET}",
            (
                "GXT_INPUTTYPES_TBL",
                42,
            ): "InputTypeId=42&Name=Delay&GenerateAlarms=true&ExitAlleyZoneDoNotTestIt=false",
            (
                "GXT_INPUTTYPES_TBL",
                50,
            ): "InputTypeId=50&Name=Delay No Test&GenerateAlarms=true&ExitAlleyZoneDoNotTestIt=true",
        }
        # Status codes as the web API reports them.
        self.areas = {0: [0, 0, 0, 0], 16: [0, 0, 0, 0]}
        self.doors = {0: [0, 0, 0], 1: [0, 0, 0]}
        self.inputs = {0: [0, 0], 14: [0, 0]}
        self.outputs = {112: [0]}
        self.troubles = {191: [0, 0]}
        self.events: list[str] = []  # waiting for the next Update
        self.controls: list[tuple[str, int, int, int | None]] = []
        self.refuse_controls = False

    # ----- helpers --------------------------------------------------------

    def log(self, line: str) -> None:
        self.events.append(f"Tue 04/11/2026 07:30:00 {line}")

    def drop_sessions(self) -> None:
        self.sessions.clear()

    def _status(self, prefix: str, table: dict[int, list[int]]) -> str:
        pairs = [f"{prefix}{rec_id}={','.join(map(str, values))}" for rec_id, values in table.items()]
        return "&".join([*pairs, f"{prefix}{NOT_SET}=0,0"])

    # ----- server ---------------------------------------------------------

    async def handle(self, request: web.Request) -> web.Response:
        query = request.query_string
        self.requests.append(query)
        cookie = request.headers.get("Cookie", "").removeprefix("SESSID=")
        if query.startswith("Command&Type=Session&SubType=InitSession"):
            self._next_session += 1
            sid = f"S{self._next_session}"
            resp = web.Response(text=str(RAND))
            resp.headers["Set-Cookie"] = f"SESSID={sid}; Path=/"
            self._pending = sid
            return resp
        if query.startswith("Command&Type=Session&SubType=CheckPasswordServer"):
            expected = login_params(USERNAME, PASSWORD, RAND)
            if (request.query.get("Name"), request.query.get("Password")) != expected:
                self.failed_logins += 1
                return web.Response(text="FAIL")
            self.sessions.add(cookie)
            self.logins += 1
            return web.Response(text="OK")
        if cookie not in self.sessions:
            return web.Response(text="<invalid session>")
        if query.startswith("Command&Type=Session&SubType=CloseSession"):
            self.sessions.discard(cookie)
            return web.Response(text="OK")
        q = request.query
        if query.startswith("Request&Type=Health&SubType=Values"):
            return web.Response(
                text="NetworkVoltage=13604&SerialNumber=C227FCC1&WXVersion=4.00 2510 P048&MainAppBuild=1543"
            )
        if query.startswith("Request&Type=List&"):
            return web.Response(text="&".join(f"{i}={n}" for i, n in self.lists[q["SubType"]]))
        if query.startswith("Request&Type=Detail&"):
            text = self.details.get((q["SubType"], int(q["RecId"])))
            return web.Response(text=text or "Request Failed")
        if query.startswith("Request&Type=Status&"):
            prefix, table = {
                "GXT_AREAS_TBL": ("Area", self.areas),
                "GXT_DOORS_TBL": ("Door", self.doors),
                "GXT_INPUTS_TBL": ("Input", self.inputs),
                "GXT_PGMS_TBL": ("Output", self.outputs),
                "GXT_TROUBLEINPUTS_TBL": ("TroubleInput", self.troubles),
            }[q["SubType"]]
            return web.Response(text=self._status(prefix, table))
        if query.startswith("Request&Type=Events&SubType=Latest"):
            self.events.clear()
            return web.Response(text="Event0=Tue 04/11/2026 07:00:00 Door Front Door Locked&EventCodes0=1")
        if query.startswith("Request&Type=Events&SubType=Update"):
            if not self.events:
                return web.Response(text="<no response>")
            events, self.events = self.events, []
            # Newest first, as the controller lists them.
            return web.Response(text="&".join(f"Event{i}={line}" for i, line in enumerate(reversed(events))))
        if query.startswith("Command&Type=Control&"):
            table, rec_id, command = q["SubType"], int(q["RecId"]), int(q.getall("Command")[-1])
            data1 = int(q["Data1"]) if "Data1" in q else None
            self.controls.append((table, rec_id, command, data1))
            if self.refuse_controls:
                return web.Response(text="Command Failed")
            if table == "GXT_DOORS_TBL":
                self.doors[rec_id][0] = {0: 0, 1: 7, 2: 9}[command]
            elif table == "GXT_AREAS_TBL":
                self.areas[rec_id][1], self.areas[rec_id][2] = (
                    (0, 0) if command in (0, 1, 2) else (128, {10: 0x40, 5: 0x20, 4: 0x10}.get(command, 0))
                )
            elif table == "GXT_PGMS_TBL":
                self.outputs[rec_id][0] = {0: 0, 1: 1, 2: 3}[command]
            elif table == "GXT_INPUTS_TBL":
                self.inputs[rec_id][1] = {0: 0, 1: 1, 2: 3}[command]
            return web.Response(text="OK")
        return web.Response(text="Request Failed")

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_route("*", "/PRT_CTRL_DIN_ISAPI.dll", self.handle)
        return app
