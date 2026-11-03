"""WX web interface name reading, against a small fake of the controller's HTTP API."""

from __future__ import annotations

import aiohttp
import pytest
from aiohttp import web

from custom_components.ict_automation.protege import Record, RecordType
from custom_components.ict_automation.protege.wx import (
    WXAuthError,
    WXClient,
    WXSite,
    apply_names,
    apply_zone_settings,
    login_params,
    parse_list,
    xor_hex,
)

RAND = 0x1234ABCD
# Input 1 is in Soundline (WX area 16) as type 5; input 14's details are hidden from the operator.
DETAILS = {
    (
        "GXT_INPUTS_TBL",
        0,
    ): "Name=Front Door Reed&Area1=0&ZoneType1=4&Area2=16&ZoneType2=5&Area3=-1&ZoneType3=-1",
    ("GXT_INPUTS_TBL", 1): "Name=Front Door PTE&Area1=16&ZoneType1=5&Area2=4294967295",
    ("GXT_INPUTTYPES_TBL", 4): "Name=Instant&AllowOpen=false",
    ("GXT_INPUTTYPES_TBL", 5): "Name=Entry Delay&AllowOpen=true",
}
LISTS = {
    "GXT_DOORS_TBL": "0=Front Door&1=Jail Gate",
    "GXT_AREAS_TBL": "0=System Area&16=Soundline",
    "GXT_INPUTS_TBL": "0=Front Door Reed - RE01.I01&1=Front Door PTE - RE01.I02&14=Gallery PIR - IE01.I01",
    "GXT_PGMS_TBL": "112=Controller Bell 0&114=Controller Output 1",
    "GXT_TROUBLEINPUTS_TBL": "191=12V Supply Failure",
}


def test_xor_hex_uses_low_byte_first():
    assert xor_hex("AB", 0x00000101) == f"{ord('A') ^ 1:02X}{ord('B') ^ 1:02X}"
    assert xor_hex("abcde", 0x04030201) == "6060606064"


def test_parse_list_sorts_and_decodes():
    assert parse_list("16=Soundline&0=System Area") == [(0, "System Area"), (16, "Soundline")]
    assert parse_list("3=Tom\xe0Jerry\r\n") == [(3, "Tom&Jerry")]


def test_names_map_by_position_and_need_matching_counts():
    records = [Record(RecordType.AREA, 1), Record(RecordType.AREA, 2), Record(RecordType.DOOR, 1)]
    lists = {RecordType.AREA: [(0, "System Area"), (16, "Soundline")], RecordType.DOOR: [(0, "A"), (1, "B")]}
    assert apply_names(records, lists) == 2
    assert [r.name for r in records] == ["System Area", "Soundline", None]


@pytest.fixture
async def wx_server(socket_enabled, aiohttp_server):
    state = {"requests": [], "logged_in": False}
    expected = login_params("testuser", "secret", RAND)

    async def handler(request: web.Request) -> web.Response:
        query = request.query_string
        state["requests"].append(query)
        if request.method != "POST":
            return web.Response(status=405)
        if query.startswith("Command&Type=Session&SubType=InitSession"):
            resp = web.Response(text=str(RAND))
            resp.headers["Set-Cookie"] = "SESSIONID=abc; Path=/"
            return resp
        if request.headers.get("Cookie") != "SESSIONID=abc":
            return web.Response(text="FAIL")
        if query.startswith("Command&Type=Session&SubType=CheckPasswordServer"):
            ok = request.query.get("Name") == expected[0] and request.query.get("Password") == expected[1]
            state["logged_in"] = ok
            return web.Response(text="OK" if ok else "FAIL")
        if query.startswith("Command&Type=Session&SubType=CloseSession"):
            state["logged_in"] = False
            return web.Response(text="OK")
        if not state["logged_in"]:
            return web.Response(text="FAIL")
        if query.startswith("Request&Type=List&SubType="):
            return web.Response(text=LISTS[request.query["SubType"]])
        if query.startswith("Request&Type=Detail&SubType="):
            table, rec_id = request.query["SubType"], int(request.query["RecId"])
            if (table, rec_id) in DETAILS:
                return web.Response(text=DETAILS[(table, rec_id)])
        return web.Response(text="Request Failed")

    app = web.Application()
    app.router.add_route("*", "/PRT_CTRL_DIN_ISAPI.dll", handler)
    server = await aiohttp_server(app)
    server.state = state
    return server


async def test_fetch_all_logs_in_lists_and_logs_out(wx_server):
    async with aiohttp.ClientSession() as session:
        client = WXClient(session, "127.0.0.1", "testuser", "secret", wx_server.port, scheme="http")
        lists = await client.fetch_all()
    assert lists[RecordType.DOOR] == [(0, "Front Door"), (1, "Jail Gate")]
    assert lists[RecordType.OUTPUT][0] == (112, "Controller Bell 0")
    sent = wx_server.state["requests"]
    assert sent[-1].startswith("Command&Type=Session&SubType=CloseSession")
    # Only session commands and List requests: nothing that could change programming.
    assert all(q.startswith(("Command&Type=Session&", "Request&Type=List&")) for q in sent)


async def test_wrong_password_is_reported(wx_server):
    async with aiohttp.ClientSession() as session:
        client = WXClient(session, "127.0.0.1", "testuser", "wrong", wx_server.port, scheme="http")
        with pytest.raises(WXAuthError):
            await client.fetch_all()


async def test_fetch_site_reads_input_details_once_and_logs_out(wx_server):
    async with aiohttp.ClientSession() as session:
        client = WXClient(session, "127.0.0.1", "testuser", "secret", wx_server.port, scheme="http")
        site = await client.fetch_site()
    assert site.inputs[0]["Area2"] == "16"
    assert site.inputs[14] == {}  # not permitted: left out, not an error
    assert sorted(site.input_types) == [4, 5]
    sent = wx_server.state["requests"]
    assert sent[-1].startswith("Command&Type=Session&SubType=CloseSession")
    assert sum(q.startswith("Command&Type=Session&SubType=InitSession") for q in sent) == 1
    assert all(
        q.startswith(("Command&Type=Session&", "Request&Type=List&", "Request&Type=Detail&")) for q in sent
    )


def test_zone_settings_map_wx_ids_to_panel_records(monkeypatch):
    from custom_components.ict_automation.protege import wx

    monkeypatch.setattr(wx, "_ARM_OPEN_FIELDS", ("AllowOpen",))
    records = [Record(RecordType.AREA, 1), Record(RecordType.AREA, 2)]
    records += [Record(RecordType.INPUT, i) for i in (1, 2, 3)]
    site = WXSite(
        lists={
            RecordType.AREA: [(0, "System Area"), (16, "Soundline")],
            RecordType.INPUT: [(0, "Reed"), (1, "PTE"), (14, "PIR")],
        },
        inputs={
            0: wx.parse_detail(DETAILS[("GXT_INPUTS_TBL", 0)]),
            1: wx.parse_detail(DETAILS[("GXT_INPUTS_TBL", 1)]),
            14: {},
        },
        input_types={
            4: wx.parse_detail(DETAILS[("GXT_INPUTTYPES_TBL", 4)]),
            5: wx.parse_detail(DETAILS[("GXT_INPUTTYPES_TBL", 5)]),
        },
    )
    assert apply_zone_settings(records, site) == 2
    reed, pte, pir = records[2:]
    assert (reed.areas, reed.arm_open) == ([1, 2], [2])
    assert (pte.areas, pte.arm_open) == ([2], [2])
    assert (pir.areas, pir.arm_open) == (None, None)


def test_zone_settings_need_matching_counts():
    records = [Record(RecordType.AREA, 1), Record(RecordType.INPUT, 1)]
    site = WXSite(
        lists={RecordType.AREA: [], RecordType.INPUT: [(0, "Reed")]},
        inputs={0: {"Area1": "0"}},
        input_types={},
    )
    assert apply_zone_settings(records, site) == 0
    assert records[1].areas is None
