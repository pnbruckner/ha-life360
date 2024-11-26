"""Test Life360 config entry migration."""
from __future__ import annotations

from collections.abc import Generator
import re
from unittest.mock import AsyncMock, patch

from custom_components.life360.config_flow import Life360ConfigFlow
from custom_components.life360.const import DOMAIN
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    assert_setup_component,
)

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from .common import assert_log_messages

MIGRATION_MESSAGE = "Migrating Life360 integration entries from version 1 to " + str(
    Life360ConfigFlow.VERSION
)

# ========== Fixtures ==================================================================


@pytest.fixture
def setup_entry_mock(hass: HomeAssistant) -> Generator[AsyncMock]:
    """Mock async_setup_entry."""
    with patch("custom_components.life360.async_setup_entry", autospec=True) as mock:
        # Teardown, in newer HA versions, will unload config entries. Add DOMAIN to
        # hass.data so real async_unload_entry won't cause an exception.
        # hass.data[DOMAIN] = None
        mock.return_value = True
        yield mock


@pytest.fixture(scope="module")
def unload_entry_mock() -> Generator[AsyncMock]:
    """Mock async_unload_entry."""
    with patch("custom_components.life360.async_unload_entry", autospec=True) as mock:
        mock.return_value = True
        yield mock


# ========== Tests =====================================================================


@pytest.mark.parametrize("bad_vers", [1, 3])
async def test_uknown_config_version(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    setup_entry_mock: AsyncMock,
    unload_entry_mock: AsyncMock,
    bad_vers: int,
) -> None:
    """Test with unknown config entry version (i.e., downgrading)."""
    entry = MockConfigEntry(domain=DOMAIN, version=bad_vers)
    entry.add_to_hass(hass)

    with assert_setup_component(0, DOMAIN):
        assert await async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done()

    pat1 = re.compile(
        r"Unsupported configuration entry found: [^,]+, version: "
        rf"{bad_vers}(.1)?; please remove it"
    )
    assert_log_messages(caplog, ((1, "ERROR", pat1),))
