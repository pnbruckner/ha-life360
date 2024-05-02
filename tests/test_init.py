"""Test Life360 __init__.py module."""
from __future__ import annotations

from collections.abc import Generator, Iterable
from itertools import chain, repeat
from math import ceil
from typing import Any
from unittest.mock import AsyncMock, patch

from custom_components.life360.const import DOMAIN
from life360 import LoginError
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    assert_setup_component,
)

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.const import EntityCategory, UnitOfSpeed
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import uuid as uuid_util
from homeassistant.util.unit_conversion import SpeedConverter

from .conftest import Life360API_Data

# ========== Fixtures ==================================================================


@pytest.fixture
def setup_entry_mock() -> Generator[AsyncMock, None, None]:
    """Mock async_setup_entry."""
    with patch("custom_components.life360.async_setup_entry", autospec=True) as mock:
        mock.return_value = True
        yield mock


# Set scope to module so fixture still remains in effect during teardown where entry
# will be unloaded if not unloaded during test.
@pytest.fixture(scope="module")
def unload_entry_mock() -> Generator[AsyncMock, None, None]:
    """Mock async_unload_entry."""
    with patch("custom_components.life360.async_unload_entry", autospec=True) as mock:
        mock.return_value = True
        yield mock


# ========== async_setup Tests: Migration ==============================================


circles = [{"id": "cid1", "name": "Circle 1"}, {"id": "cid2", "name": "Circle 2"}]
member1 = {
    "id": "mid1",
    "firstName": "First1",
    "lastName": "Last1",
    "avatar": None,
    "features": {"shareLocation": 0},
    "loc": None,
}
member2 = {
    "id": "mid2",
    "firstName": "First2",
    "lastName": "Last2",
    "avatar": None,
    "features": {"shareLocation": 0},
    "loc": None,
}
circle_members = {"cid1": [member1], "cid2": [member2]}
members = {"cid1": {"mid1": member1}, "cid2": {"mid2": member2}}


def get_circle_members(
    cid: str, *, raise_not_modified: bool = False
) -> list[dict[str, Any]]:
    """Get details for Members in given Circle."""
    return circle_members[cid]


def get_circle_member(
    cid: str, mid: str, *, raise_not_modified: bool = False
) -> dict[str, Any]:
    """Get details for Member as seen from given Circle."""
    return members[cid][mid]


def api_data() -> Life360API_Data:
    """Generate Life360 API data."""
    return {
        "Account 1": {
            "get_circles": chain(repeat(LoginError("TEST"), 1), repeat(circles)),
            "get_circle_members": get_circle_members,
            "get_circle_member": get_circle_member,
        },
    }


# fmt: off
@pytest.mark.parametrize(
    ("MockLife360", "metric", "option_set"),
    [
        (
            api_data(),
            False,
            (
                # driving_speed, max_gps_accuracy, driving
                (None, None, False),
            ),
        ),
        (
            api_data(),
            False,
            (
                (10.0, 50.0, True),
            ),
        ),
        (
            api_data(),
            True,
            (
                (10.0, 50.0, True),
            ),
        ),
        (
            api_data(),
            False,
            (
                (None, None, False),
                (10.0, 50.0, False),
            ),
        ),
        (
            api_data(),
            False,
            (
                (None, None, False),
                (10.0, 50.0, True),
                (15.0, 100.0, False),
            ),
        ),
    ],
    indirect=["MockLife360"],
)
# fmt: on
async def test_migration(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    caplog: pytest.LogCaptureFixture,
    setup_entry_mock: AsyncMock,
    unload_entry_mock: AsyncMock,
    metric: bool,
    option_set: Iterable[tuple[float, float, bool]],
) -> None:
    """Test config entry migration."""

    if not metric:
        await hass.config.async_update(unit_system="us_customary")
        await hass.async_block_till_done()

    # Mock v1 config entries w/ associated entities.

    un_f = lambda i: f"user_{i}@email_{i}.com"
    pw_f = lambda i: f"password_{i}"
    au_f = lambda i: f"authorization_{i}"

    cfg_accts: dict[str, dict[str, Any]] = {}
    comb_ds: float | None = None
    comb_ma: int | None = None
    comb_dr: bool = False
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
            data={"username": un, "password": pw, "authorization": au},
            options={"driving_speed": ds, "max_gps_accuracy": ma, "driving": dr},
        )
        v1_entry.add_to_hass(hass)
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
        await async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done()

    # Check that v2 config entry was created and v1 config entries are gone.
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    v2_entry = entries[0]
    assert v2_entry.version == 2
    assert v2_entry.entry_id not in v1_entry_ids
    # Check that v2 config entry got setup.
    setup_entry_mock.assert_called_once_with(hass, v2_entry)

    # Check v2 config entry options.
    if comb_ds is not None and metric:
        comb_ds = SpeedConverter.convert(
            comb_ds,
            UnitOfSpeed.KILOMETERS_PER_HOUR,
            UnitOfSpeed.MILES_PER_HOUR,
        )
    assert v2_entry.options == {
        "accounts": cfg_accts,
        "driving_speed": comb_ds,
        "max_gps_accuracy": comb_ma,
        "driving": comb_dr,
        "verbosity": 0,
    }

    # Check that entities have been reassigned to v2 config entry.
    for entity_id in entity_ids:
        entity = entity_registry.async_get(entity_id)
        assert entity
        assert entity.config_entry_id == v2_entry.entry_id

    # Check that a warning message was created noting the migration.
    assert any(
        rec.levelname == "WARNING"
        and "Migrating Life360 integration entries from version 1 to 2" in rec.message
        for rec in caplog.get_records("call")
    )
