"""Check actual Home Assistant member entity names."""

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
async def test_member_naming(hass, entity_registry):
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

    for platform, suffix in [
        ("sensor", "bat_lvl"),
        ("sensor", "spd"),
        ("binary_sensor", "drv"),
    ]:
        unique_id = f"mid1-{suffix}"
        entity_id = entity_registry.async_get_entity_id(platform, DOMAIN, unique_id)
        assert entity_id, f"Missing entity: {unique_id}"

        state = hass.states.get(entity_id)
        assert state is not None

        expected_names = {
            "bat_lvl": "Life360 First1 Last1 battery level",
            "spd": "Life360 First1 Last1 Speed",
            "drv": "Life360 First1 Last1 Driving",
        }
        assert state.attributes["friendly_name"] == expected_names[suffix]

        print(
            f"\n{unique_id}"
            f"\n  entity_id: {entity_id}"
            f"\n  friendly_name: {state.attributes.get('friendly_name')}"
        )
