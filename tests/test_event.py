"""Event classification, using event texts captured on site."""

from __future__ import annotations

import pytest

from custom_components.ict_automation.event import classify
from custom_components.ict_automation.protege import protocol as p
from custom_components.ict_automation.protege.models import decode_event


def _event(raw: bytes):
    return decode_event(p.ICTDataBlock(0x30, 0x01, raw))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"Input\x00Front Door PTE - RE01.I02 Opened\x00\x00", "input"),
        (b"Door\x00Front Door Unlocked By REX \x00", "door"),
        (b"Output\x00Controller Output 1 ON By Door Front Door (LOCK)\x00", "output"),
        (b"User\x00Test User Logged Out At\x00", "user"),
        (b"User\x00Warren Entry Granted D2 - Cafe\x00", "access_granted"),
        (b"Door\x00Front Door Access Denied\x00", "access_denied"),
        (b"Area\x00Soundline Disarmed By User Jordan\x00", "disarmed"),
        (b"Input\x00Glass Break - RE01.I08 Alarm\x00", "alarm"),
        (b"Thu 10:48:54 User Warren Entry Granted D2\x00", "access_granted"),
    ],
)
def test_classify_site_events(raw, expected):
    event = _event(raw)
    assert classify(event) == expected


def test_category_and_description_are_split():
    event = _event(b"Door\x00Front Door Unlocked By REX \x00")
    assert event.category == "Door"
    assert event.description == "Front Door Unlocked By REX"
    assert event.text == "Door Front Door Unlocked By REX"


@pytest.mark.parametrize(
    ("raw", "user"),
    [
        (b"User\x00Savant Unlocked Door Jail Gate At Savant\x00", "Savant"),
        (b"User\x00Test User Logged Out At\x00", "Test User"),
        (b"User\x00Savant Remote Login Savant\x00", "Savant"),
        (b"\x00Jordan McDonald Unlocked Door Jail Gate \x00", "Jordan McDonald"),
        (b"Thu 10:48:54 User Warren Entry Granted D2\x00", None),
        (b"Door\x00Front Door Unlocked By REX\x00", None),
    ],
)
def test_user_is_extracted(raw, user):
    assert _event(raw).user == user


def test_card_number_is_left_out():
    event = _event(b"User\x00Mikey Entry Granted Front Door Using User By Card [Card 321:5]\x00")
    assert event.user == "Mikey"
    assert "321" not in event.text
    assert event.text == "User Mikey Entry Granted Front Door Using User By Card"
