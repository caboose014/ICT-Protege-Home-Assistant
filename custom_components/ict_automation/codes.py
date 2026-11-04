"""Arming codes for web-only mode.

The web interface arms as the operator and never sees a code, so in web-only mode the
integration checks codes itself. They are stored only as salted PBKDF2 hashes.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Any

ITERATIONS = 100_000


def _digest(code: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", code.encode(), bytes.fromhex(salt), ITERATIONS).hex()


def make_code(name: str, code: str) -> dict[str, Any]:
    salt = secrets.token_hex(16)
    return {"name": name, "salt": salt, "hash": _digest(code, salt)}


def match_code(codes: list[dict[str, Any]], code: str | None) -> str | None:
    """The name of the person whose code this is, or None."""
    if not code:
        return None
    for item in codes:
        if hmac.compare_digest(_digest(code, item["salt"]), item["hash"]):
            return item["name"]
    return None
