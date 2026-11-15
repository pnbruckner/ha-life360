"""Test Life360 member entities updating through Home Assistant."""

import asyncio
from itertools import repeat

import pytest
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    assert_setup_component,
)

from custom_components.life360.config_flow import Life360ConfigFlow
from custom_components.life360.const import DOMAIN

from .test_init import cfg_options, cir1, mem1
from .test_member_binary_metrics import make_member_data


@pytest.mark.parametrize(
    "MockLife360",
    [
        {
            "aid1": {
                "get_circles": repeat([cir1]),
                "get_circle_members": repeat([mem1]),
                "get_circle_member": repeat(mem1),
            }
        }
    ],
    indirect=True,
)
async def test_member_entities_update(hass, entity_registry):
    """Existing coordinator updates should refresh all member entities."""

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=Life360ConfigFlow.VERSION,
        options=cfg_options(1, verbosity=3),
    )
    entry.add_to_hass(hass)

    with assert_setup_component(0, DOMAIN):
        assert await async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done()
        await asyncio.sleep(0.1)

    def state(platform, key):
        unique_id = f"mid1-{key}"
        entity_id = entity_registry.async_get_entity_id(platform, DOMAIN, unique_id)
        assert entity_id, f"Entity missing: {unique_id}"
        entity_state = hass.states.get(entity_id)
        assert entity_state is not None
        return entity_state.state

    # Simulate an update using already-fetched member data.
    data = make_member_data()
    data.details.name = "First1 Last1"

    coordinator = entry.runtime_data.mem_coordinator["mid1"]
    coordinator.async_set_updated_data(data)
    await hass.async_block_till_done()

    assert float(state("sensor", "bat_lvl")) == 75.0
    assert float(state("sensor", "spd")) == pytest.approx(22.0 * 1.609344)
    assert state("binary_sensor", "drv") == "off"
    assert state("binary_sensor", "bat_chg") == "on"
    assert state("binary_sensor", "wifi_on") == "off"

    # Changing the driving threshold should update the entity
    # without manually refreshing its member coordinator.
    hass.config_entries.async_update_entry(
        entry,
        options=cfg_options(1, verbosity=3, driving_speed=20.0),
    )
    await hass.async_block_till_done()

    assert state("binary_sensor", "drv") == "on"

    # A member stops reporting location data.
    # Existing entities should no longer display stale readings.
    missing_location = make_member_data()
    missing_location.details.name = "First1 Last1"
    missing_location.loc = None

    coordinator.async_set_updated_data(missing_location)
    await hass.async_block_till_done()

    for key in (
        "bat_lvl",
        "spd",
        "gps_acc",
        "addr",
        "plc",
        "loc_since",
        "last_seen",
    ):
        assert state("sensor", key) == "unknown", key

    for key in ("drv", "bat_chg", "wifi_on"):
        assert state("binary_sensor", key) == "unknown", key
