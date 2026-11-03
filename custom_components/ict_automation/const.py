"""Constants for the ICT Protege integration."""

from __future__ import annotations

DOMAIN = "ict_automation"
MANUFACTURER = "Integrated Control Technology"

CONF_PIN = "pin"
CONF_RECORDS = "records"
CONF_EVENTS_PORT = "events_port"
CONF_EVENTS_PIN = "events_pin"
CONF_WX_USERNAME = "wx_username"
CONF_WX_PASSWORD = "wx_password"
CONF_WX_PORT = "wx_port"

DEFAULT_WX_PORT = 443

ARM_MODES = ["away", "home", "night"]

DEFAULT_PORT = 21000

EVENT_PANEL = f"{DOMAIN}_event"
EVENT_EMERGENCY = f"{DOMAIN}_emergency"

EVENT_TYPES = [
    "access_granted",
    "access_denied",
    "armed",
    "disarmed",
    "alarm",
    "door",
    "input",
    "output",
    "user",
    "trouble",
    "other",
]
