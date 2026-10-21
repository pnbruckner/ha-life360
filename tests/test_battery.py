"""Test battery sensors and compatibility with tracker battery attributes."""

from __future__ import annotations

import importlib
from copy import deepcopy
from itertools import repeat
from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    ATTR_BATTERY_LEVEL,
    ATTR_DEVICE_CLASS,
    ATTR_FRIENDLY_NAME,
    ATTR_UNIT_OF_MEASUREMENT,
    PERCENTAGE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.life360.config_flow import Life360ConfigFlow
from custom_components.life360.const import DOMAIN

from .test_init import cfg_options

MEMBER: dict[str, Any] = {
    "id": "mid1",
    "firstName": "First",
    "lastName": "Last",
    "avatar": None,
    "features": {"shareLocation": 1},
    "issues": {"title": "", "dialog": ""},
    "location": {
        "battery": "60",
        "charge": "0",
        "wifiState": "1",
        "address1": "Home",
        "address2": "",
        "since": "1792497600",
        "timestamp": "1792497600",
        "isDriving": "0",
        "accuracy": "20",
        "latitude": "40",
        "longitude": "-75",
        "name": "Home",
        "speed": "0",
    },
}

pytestmark = pytest.mark.parametrize(
    "MockLife360",
    [
        {
            "aid1": {
                "get_circles": repeat([{"id": "cid1", "name": "Circle"}]),
                "get_circle_members": repeat([MEMBER]),
                "get_circle_member": repeat(MEMBER),
            }
        }
    ],
    indirect=True,
)


@pytest.fixture
async def battery_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Set up a member with a known battery level."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=Life360ConfigFlow.VERSION,
        options=cfg_options(1, verbosity=3),
    )
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry


async def test_battery_sensor_and_tracker_attribute(
    hass: HomeAssistant,
    battery_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    MockLife360: MagicMock,
) -> None:
    """Expose a battery sensor while preserving the tracker attribute and ID."""
    sensor_id = entity_registry.async_get_entity_id("sensor", DOMAIN, "mid1_battery")
    assert sensor_id == "sensor.life360_first_last_battery"
    state = hass.states.get(sensor_id)
    assert state is not None
    assert state.state == "60"
    assert state.attributes[ATTR_DEVICE_CLASS] == SensorDeviceClass.BATTERY
    assert state.attributes["state_class"] == SensorStateClass.MEASUREMENT
    assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == PERCENTAGE

    tracker_id = entity_registry.async_get_entity_id("device_tracker", DOMAIN, "mid1")
    assert tracker_id == "device_tracker.life360_first_last"
    tracker_state = hass.states.get(tracker_id)
    assert tracker_state is not None
    assert tracker_state.attributes[ATTR_BATTERY_LEVEL] == 60
    # Both entities share the initial member request.
    assert MockLife360.apis[0].get_circle_member.await_count == 1


@pytest.mark.parametrize(
    ("battery", "expected"),
    [("0", "0"), ("55", "55"), ("100", "100"), ("-1", STATE_UNKNOWN)],
)
async def test_battery_updates(
    hass: HomeAssistant,
    battery_entry: MockConfigEntry,
    MockLife360: MagicMock,
    battery: str,
    expected: str,
) -> None:
    """Update both entities from one request, including zero and unknown values."""
    member = deepcopy(MEMBER)
    member["location"]["battery"] = battery
    api = MockLife360.apis[0]
    api.get_circle_member.side_effect = None
    api.get_circle_member.return_value = member
    calls = api.get_circle_member.await_count

    await battery_entry.runtime_data.mem_coordinator["mid1"].async_refresh()
    await hass.async_block_till_done()

    state = hass.states.get("sensor.life360_first_last_battery")
    assert state is not None
    assert state.state == expected
    tracker_state = hass.states.get("device_tracker.life360_first_last")
    assert tracker_state is not None
    if expected == STATE_UNKNOWN:
        assert ATTR_BATTERY_LEVEL not in tracker_state.attributes
    else:
        assert tracker_state.attributes[ATTR_BATTERY_LEVEL] == int(expected)
    assert api.get_circle_member.await_count == calls + 1


@pytest.mark.parametrize("missing", ["location", "sharing"])
async def test_missing_member_location(
    hass: HomeAssistant,
    battery_entry: MockConfigEntry,
    MockLife360: MagicMock,
    missing: str,
) -> None:
    """Clear the battery when location is missing or sharing is disabled."""
    member = deepcopy(MEMBER)
    if missing == "location":
        member["location"] = None
    else:
        member["features"]["shareLocation"] = 0
    api = MockLife360.apis[0]
    api.get_circle_member.side_effect = None
    api.get_circle_member.return_value = member

    await battery_entry.runtime_data.mem_coordinator["mid1"].async_refresh()
    await hass.async_block_till_done()

    state = hass.states.get("sensor.life360_first_last_battery")
    assert state is not None
    assert state.state == STATE_UNKNOWN
    tracker_state = hass.states.get("device_tracker.life360_first_last")
    assert tracker_state is not None
    assert ATTR_BATTERY_LEVEL not in tracker_state.attributes


async def test_member_renamed(
    hass: HomeAssistant,
    battery_entry: MockConfigEntry,
    MockLife360: MagicMock,
) -> None:
    """Keep the sensor ID when a member changes their name."""
    member = deepcopy(MEMBER)
    member["firstName"] = "Updated"
    api = MockLife360.apis[0]
    api.get_circle_member.side_effect = None
    api.get_circle_member.return_value = member

    await battery_entry.runtime_data.mem_coordinator["mid1"].async_refresh()
    await hass.async_block_till_done()

    state = hass.states.get("sensor.life360_first_last_battery")
    assert state is not None
    assert state.attributes[ATTR_FRIENDLY_NAME] == "Life360 Updated Last battery"


async def test_battery_sensor_reload(
    hass: HomeAssistant,
    battery_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Reload without duplicating sensors, then mark them unavailable on unload."""
    assert await hass.config_entries.async_reload(battery_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    sensors = [
        entity
        for entity in entity_registry.entities.values()
        if entity.platform == DOMAIN and entity.domain == "sensor"
    ]
    assert len(sensors) == 1
    state = hass.states.get(sensors[0].entity_id)
    assert state is not None
    assert state.state == "60"

    assert await hass.config_entries.async_unload(battery_entry.entry_id)
    await hass.async_block_till_done()
    state = hass.states.get(sensors[0].entity_id)
    assert state is not None
    assert state.state == STATE_UNAVAILABLE


async def test_member_added_and_removed(
    hass: HomeAssistant,
    battery_entry: MockConfigEntry,
    MockLife360: MagicMock,
) -> None:
    """Remove and recreate a battery sensor when circle membership changes."""
    api = MockLife360.apis[0]
    api.get_circle_members.side_effect = None
    api.get_circle_members.return_value = []
    await battery_entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    state = hass.states.get("sensor.life360_first_last_battery")
    assert state is not None
    assert state.state == STATE_UNAVAILABLE

    api.get_circle_members.return_value = [MEMBER]
    await battery_entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    state = hass.states.get("sensor.life360_first_last_battery")
    assert state is not None
    assert state.state == "60"


async def test_unknown_battery_recovers(
    hass: HomeAssistant,
    battery_entry: MockConfigEntry,
    MockLife360: MagicMock,
) -> None:
    """Resume reporting battery level when Life360 sends a valid value again."""
    api = MockLife360.apis[0]
    api.get_circle_member.side_effect = None
    for battery, expected in (("-1", STATE_UNKNOWN), ("60", "60")):
        member = deepcopy(MEMBER)
        member["location"]["battery"] = battery
        api.get_circle_member.return_value = member
        await battery_entry.runtime_data.mem_coordinator["mid1"].async_refresh()
        await hass.async_block_till_done()
        state = hass.states.get("sensor.life360_first_last_battery")
        assert state is not None
        assert state.state == expected


async def test_no_deprecated_battery_override(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Importing the tracker must not trigger Home Assistant's battery warning."""
    from custom_components.life360 import device_tracker

    caplog.clear()
    importlib.reload(device_tracker)
    assert "overriding the deprecated battery_level property" not in caplog.text
