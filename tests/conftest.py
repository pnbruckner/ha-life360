"""Life360 test configuration."""
from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations."""
    return


@pytest.fixture(autouse=True)
def life360_api() -> Generator[AsyncMock, None, None]:
    """Mock Life360 API."""
    with patch("custom_components.life360.helpers.Life360", autospec=True) as mock:
        yield mock.return_value
