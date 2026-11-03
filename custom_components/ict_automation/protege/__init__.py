"""Async client for ICT Protege panels over the Automation and Control Service.

This package has no Home Assistant imports so it can be published separately.
"""

from .client import (
    CannotConnect,
    CommandRejected,
    InvalidPin,
    ProtegeClient,
    ProtegeError,
    ProtegeEventClient,
    ProtegeTimeout,
)
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
    StatusUpdate,
)

__all__ = [
    "AreaState",
    "AreaStatus",
    "CannotConnect",
    "CommandRejected",
    "DoorLockState",
    "DoorState",
    "DoorStatus",
    "InputState",
    "InputStatus",
    "InvalidPin",
    "OutputState",
    "OutputStatus",
    "PanelEvent",
    "PanelInfo",
    "ProtegeClient",
    "ProtegeError",
    "ProtegeEventClient",
    "ProtegeTimeout",
    "Record",
    "RecordType",
    "StatusUpdate",
]
