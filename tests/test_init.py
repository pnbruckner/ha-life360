"""Test Life360 __init__.py module."""
from __future__ import annotations

from collections.abc import Iterable
from math import ceil
from typing import Any

from custom_components.life360.const import DOMAIN
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
async def test_migration(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    entity_registry: er.EntityRegistry,
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
