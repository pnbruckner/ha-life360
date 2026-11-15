"""Resilience tests for Life360 member metrics."""

import asyncio
from itertools import repeat

import pytest
from homeassistant.helpers.entity_registry import RegistryEntryDisabler
from homeassistant.helpers.update_coordinator import UpdateFailed
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
async def test_failure_recovery_and_option_changes(hass, entity_registry):
    """Metrics recover from errors and reflect repeated option changes."""

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
        eid = entity_registry.async_get_entity_id(platform, DOMAIN, f"mid1-{key}")
        assert eid is not None
        result = hass.states.get(eid)
        assert result is not None
        return result.state

    coordinator = entry.runtime_data.mem_coordinator["mid1"]

    data = make_member_data()
    data.details.name = "First1 Last1"
    coordinator.async_set_updated_data(data)
    await hass.async_block_till_done()

    assert float(state("sensor", "bat_lvl")) == 75
    assert state("binary_sensor", "bat_chg") == "on"

    # An actual coordinator failure should mark its metrics unavailable.
    coordinator.async_set_update_error(UpdateFailed("Simulated Life360 failure"))
    await hass.async_block_till_done()

    for platform, key in (
        ("sensor", "bat_lvl"),
        ("sensor", "spd"),
        ("binary_sensor", "drv"),
        ("binary_sensor", "wifi_on"),
    ):
        assert state(platform, key) == "unavailable", (platform, key)

    # New coordinator data should recover those same entities.
    data.loc.battery_level = 0
    data.loc.details.speed = 18
    data.loc.battery_charging = False
    data.loc.wifi_on = True

    coordinator.async_set_updated_data(data)
    await hass.async_block_till_done()

    assert float(state("sensor", "bat_lvl")) == 0
    assert state("binary_sensor", "bat_chg") == "off"
    assert state("binary_sensor", "wifi_on") == "on"
    assert state("binary_sensor", "drv") == "off"

    # Verify repeated configuration changes without coordinator refresh.
    for threshold, expected in (
        (19.0, "off"),
        (18.0, "on"),
        (None, "off"),
    ):
        hass.config_entries.async_update_entry(
            entry,
            options=cfg_options(1, verbosity=3, driving_speed=threshold),
        )
        await hass.async_block_till_done()

        assert state("binary_sensor", "drv") == expected


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
async def test_disabled_entities_reload_and_reenable(hass, entity_registry):
    """User-disabled metrics retain identity across reload and re-enable."""

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

    wifi_id = entity_registry.async_get_entity_id(
        "binary_sensor", DOMAIN, "mid1-wifi_on"
    )
    battery_id = entity_registry.async_get_entity_id("sensor", DOMAIN, "mid1-bat_lvl")
    tracker_id = entity_registry.async_get_entity_id("device_tracker", DOMAIN, "mid1")

    assert wifi_id and battery_id and tracker_id

    device_id = entity_registry.async_get(tracker_id).device_id
    assert device_id is not None

    # Disable one metric the way a Home Assistant user would.
    entity_registry.async_update_entity(wifi_id, disabled_by=RegistryEntryDisabler.USER)
    await hass.async_block_till_done()

    assert entity_registry.async_get(wifi_id).disabled_by == RegistryEntryDisabler.USER

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await asyncio.sleep(0.1)

    assert (
        entity_registry.async_get_entity_id("binary_sensor", DOMAIN, "mid1-wifi_on")
        == wifi_id
    )

    assert entity_registry.async_get(wifi_id).disabled_by == RegistryEntryDisabler.USER

    # Enabled metrics still function after reload.
    battery_state = hass.states.get(battery_id)
    assert battery_state is not None
    assert battery_state.state != "unavailable"

    # Re-enable the disabled metric and reload.
    entity_registry.async_update_entity(wifi_id, disabled_by=None)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await asyncio.sleep(0.1)

    assert (
        entity_registry.async_get_entity_id("binary_sensor", DOMAIN, "mid1-wifi_on")
        == wifi_id
    )

    wifi_state = hass.states.get(wifi_id)
    assert wifi_state is not None
    assert wifi_state.state != "unavailable"
    assert wifi_state.attributes.get("restored") is not True

    # Device grouping survives the entire lifecycle.
    for eid in (wifi_id, battery_id, tracker_id):
        assert entity_registry.async_get(eid).device_id == device_id
