"""Shared fixtures."""

from __future__ import annotations

import pytest

from .fake_panel import FakePanel


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load custom_components in every test."""
    yield


@pytest.fixture
async def panel(socket_enabled):
    fake = FakePanel()
    await fake.start()
    yield fake
    await fake.stop()
