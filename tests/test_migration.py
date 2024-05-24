"""Test Life360 config entry migration."""
from __future__ import annotations

from collections.abc import Generator, Iterable
from math import ceil
import re
from typing import Any
from unittest.mock import AsyncMock, patch

from custom_components.life360.config_flow import Life360ConfigFlow
from custom_components.life360.const import DOMAIN
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    assert_setup_component,
)

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfSpeed
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import uuid as uuid_util
from homeassistant.util.unit_conversion import SpeedConverter

from .common import assert_log_messages

MIGRATION_MESSAGE = "Migrating Life360 integration entries from version 1 to " + str(
    Life360ConfigFlow.VERSION
)

# ========== Fixtures ==================================================================


@pytest.fixture
def setup_entry_mock(hass: HomeAssistant) -> Generator[AsyncMock, None, None]:
    """Mock async_setup_entry."""
    with patch("custom_components.life360.async_setup_entry", autospec=True) as mock:
        # Teardown, in newer HA versions, will unload config entries. Add DOMAIN to
        # hass.data so real async_unload_entry won't cause an exception.
        # hass.data[DOMAIN] = None
        mock.return_value = True
        yield mock


@pytest.fixture(scope="module")
def unload_entry_mock() -> Generator[AsyncMock, None, None]:
    """Mock async_unload_entry."""
    with patch("custom_components.life360.async_unload_entry", autospec=True) as mock:
        mock.return_value = True
        yield mock


@pytest.fixture
def remove_entry_mock() -> Generator[AsyncMock, None, None]:
    """Mock async_remove_entry."""
    with patch("custom_components.life360.async_remove_entry", autospec=True) as mock:
        mock.return_value = True
        yield mock


# ========== Tests =====================================================================


# fmt: off
@pytest.mark.parametrize(
    ("metric", "option_set"),
    [
        (
            False,
            (
                # driving_speed, max_gps_accuracy, driving
                (None, None, False),
            ),
        ),
        (
            False,
            (
                (10.0, 50.0, True),
            ),
        ),
        (
            True,
            (
                (10.0, 50.0, True),
            ),
        ),
        (
            False,
            (
                (None, None, False),
                (10.0, 50.0, False),
            ),
        ),
        (
            False,
            (
                (None, None, False),
                (10.0, 50.0, True),
                (15.0, 100.0, False),
            ),
        ),
    ],
)
# fmt: on
async def test_migration_v1(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    caplog: pytest.LogCaptureFixture,
    setup_entry_mock: AsyncMock,
    unload_entry_mock: AsyncMock,
    remove_entry_mock: AsyncMock,
    metric: bool,
    option_set: Iterable[tuple[float, float, bool]],
) -> None:
    """Test config entry migration from version 1."""
    if not metric:
        await hass.config.async_update(unit_system="us_customary")
        await hass.async_block_till_done()

    # Mock v1 config entries w/ associated entities.

    un_f = lambda i: f"user_{i}@email_{i}.com"  # noqa: E731
    pw_f = lambda i: f"password_{i}"  # noqa: E731
    au_f = lambda i: f"authorization_{i}"  # noqa: E731

    cfg_accts: dict[str, dict[str, Any]] = {}
    comb_ds: float | None = None
    comb_ma: int | None = None
    comb_dr: bool = False
    v1_entries: list[ConfigEntry] = []
    v1_entry_ids: list[str] = []
    entity_ids: list[str] = []
    for i, o in enumerate(option_set):
        un = un_f(i)
        pw = pw_f(i)
        au = au_f(i)
        ds, ma, dr = o

        cfg_accts[un] = {"password": pw, "authorization": au, "enabled": True}
        if ds is not None:
            if comb_ds is None:
                comb_ds = ds
            else:
                comb_ds = min(comb_ds, ds)
        if ma is not None:
            ima = ceil(ma)
            if comb_ma is None:
                comb_ma = ima
            else:
                comb_ma = max(comb_ma, ima)
        comb_dr |= dr

        v1_entry = MockConfigEntry(
            domain=DOMAIN,
            version=1,
            data={"username": un, "password": pw, "authorization": au},
            options={"driving_speed": ds, "max_gps_accuracy": ma, "driving": dr},
        )
        v1_entry.add_to_hass(hass)
        v1_entries.append(v1_entry)
        v1_entry_ids.append(v1_entry.entry_id)

        name = f"life360 online ({un})"
        entity_ids.append(
            entity_registry.async_get_or_create(
                "binary_sensor",
                DOMAIN,
                un,
                suggested_object_id=name,
                config_entry=v1_entry,
                original_device_class=BinarySensorDeviceClass.CONNECTIVITY,
                original_name=name,
            ).entity_id
        )

        name = f"User {i}"
        entity_ids.append(
            entity_registry.async_get_or_create(
                "device_tracker",
                DOMAIN,
                uuid_util.random_uuid_hex(),
                suggested_object_id=name,
                config_entry=v1_entry,
                entity_category=EntityCategory.DIAGNOSTIC,
                original_name=name,
            ).entity_id
        )

    with assert_setup_component(0, DOMAIN):
        assert await async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done()

    # Check that new config entry was created and v1 config entries are gone.
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    new_entry = entries[0]
    assert new_entry.version == Life360ConfigFlow.VERSION
    assert new_entry.entry_id not in v1_entry_ids
    # Check that v1 config entries got removed.
    for entry in v1_entries:
        remove_entry_mock.assert_any_call(hass, entry)
    # Check that new config entry got setup.
    setup_entry_mock.assert_called_once_with(hass, new_entry)

    # Check new config entry options.
    if comb_ds is not None and metric:
        comb_ds = SpeedConverter.convert(
            comb_ds,
            UnitOfSpeed.KILOMETERS_PER_HOUR,
            UnitOfSpeed.MILES_PER_HOUR,
        )
    assert new_entry.options == {
        "accounts": cfg_accts,
        "driving_speed": comb_ds,
        "max_gps_accuracy": comb_ma,
        "driving": comb_dr,
        "verbosity": 0,
    }

    # Check that entities have been reassigned to new config entry.
    for entity_id in entity_ids:
        entity = entity_registry.async_get(entity_id)
        assert entity
        assert entity.config_entry_id == new_entry.entry_id

    # Check that a warning message was created noting the migration.
    assert_log_messages(caplog, ((1, "WARNING", MIGRATION_MESSAGE),))


@pytest.mark.parametrize("entity_migrated", [False, True])
async def test_aborted_migration_v1(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    caplog: pytest.LogCaptureFixture,
    setup_entry_mock: AsyncMock,
    unload_entry_mock: AsyncMock,
    remove_entry_mock: AsyncMock,
    entity_migrated: bool,
) -> None:
    """Test aborted & restarted config entry migration from verstion 1."""
    # Set up partial migration scenario with new entry created, but v1 entry still
    # still present and entity possibly still associated with v1 entry.
    v1_entry = MockConfigEntry(domain=DOMAIN, version=1)
    v1_entry.add_to_hass(hass)
    new_entry = MockConfigEntry(domain=DOMAIN, version=Life360ConfigFlow.VERSION)
    new_entry.add_to_hass(hass)
    entity_id = entity_registry.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        "user@email.com",
        config_entry=new_entry if entity_migrated else v1_entry,
        original_device_class=BinarySensorDeviceClass.CONNECTIVITY,
    ).entity_id

    with assert_setup_component(0, DOMAIN):
        assert await async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done()

    # Check that v1 config entry is gone.
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    assert entries[0] is new_entry
    # Check that v1 config entry got removed.
    remove_entry_mock.assert_called_once_with(hass, v1_entry)
    # Check that new config entry got setup.
    setup_entry_mock.assert_called_once_with(hass, new_entry)

    # Check that entity is associated with new config entry.
    entity = entity_registry.async_get(entity_id)
    assert entity
    assert entity.config_entry_id == new_entry.entry_id

    # Check that a warning message was NOT created noting the migration.
    assert_log_messages(caplog, ((0, "WARNING", MIGRATION_MESSAGE),))


async def test_uknown_config_version(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    setup_entry_mock: AsyncMock,
    unload_entry_mock: AsyncMock,
) -> None:
    """Test with unknown config entry version (i.e., downgrading)."""
    bad_vers = Life360ConfigFlow.VERSION + 1
    entry = MockConfigEntry(domain=DOMAIN, version=bad_vers)
    entry.add_to_hass(hass)

    with assert_setup_component(0, DOMAIN):
        assert not await async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done()

    pat1 = re.compile(
        rf"Unsupported configuration entry found: [^,]+, version: {bad_vers}(.1)?"
    )
    pat2 = re.compile(
        r"Setup failed for custom integration '?life360'?"
        r": Integration failed to initialize\."
    )
    assert_log_messages(caplog, ((1, "ERROR", pat1), (1, "ERROR", pat2)))
