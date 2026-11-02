"""Constants for the ICT Protege integration."""

from __future__ import annotations

DOMAIN = "ict_automation"
MANUFACTURER = "Integrated Control Technology"

CONF_PIN = "pin"
CONF_RECORDS = "records"
CONF_EVENTS_PORT = "events_port"
CONF_EVENTS_PIN = "events_pin"

DEFAULT_PORT = 21000

EVENT_PANEL = f"{DOMAIN}_event"

EVENT_TYPES = [
    "access_granted",
    "access_denied",
    "armed",
    "disarmed",
    "alarm",
    "door",
    "trouble",
    "other",
]
