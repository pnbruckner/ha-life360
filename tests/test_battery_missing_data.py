"""Regression tests for missing and invalid Life360 battery readings."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml
from homeassistant.const import (
    ATTR_BATTERY_LEVEL,
    EVENT_HOMEASSISTANT_START,
    EVENT_HOMEASSISTANT_STARTED,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import CoreState, HomeAssistant, State
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)

from . import test_battery
from .test_battery import MEMBER

battery_entry = test_battery.battery_entry
pytestmark = test_battery.pytestmark


async def update_member(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mock_api: MagicMock,
    member: dict[str, Any],
) -> None:
    """Feed a member response through the real coordinator and entities."""
    mock_api.get_circle_member.side_effect = None
    mock_api.get_circle_member.return_value = member
    await entry.runtime_data.mem_coordinator["mid1"].async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_every_valid_battery_percentage(
    hass: HomeAssistant, battery_entry: MockConfigEntry, MockLife360: MagicMock
) -> None:
    """No valid percentage, including 55 to 60, should lose the battery reading."""
    for battery in range(100, -1, -1):
        member = deepcopy(MEMBER)
        member["location"]["battery"] = str(battery)
        await update_member(hass, battery_entry, MockLife360.apis[0], member)
        sensor = hass.states.get("sensor.life360_first_last_battery")
        tracker = hass.states.get("device_tracker.life360_first_last")
        assert sensor is not None and sensor.state == str(battery)
        assert tracker is not None
        assert tracker.attributes[ATTR_BATTERY_LEVEL] == battery


async def test_reported_template_during_battery_gap(
    hass: HomeAssistant, battery_entry: MockConfigEntry, MockLife360: MagicMock
) -> None:
    """The reporter's exact template produces zero when the attribute disappears."""
    assert await async_setup_component(
        hass,
        "template",
        {
            "template": [
                {
                    "sensor": [
                        {
                            "name": "Issue 106 original template",
                            "state": "{{ state_attr('device_tracker.life360_first_last', 'battery_level') | int(0) }}",
                            "unit_of_measurement": "%",
                        }
                    ]
                }
            ]
        },
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    for index, battery in enumerate((65, 60, -1, -1, 55, 54)):
        member = deepcopy(MEMBER)
        member["location"]["battery"] = str(battery)
        member["location"]["timestamp"] = str(1792497600 + index)
        member["location"]["latitude"] = str(40 + index / 100)
        await update_member(hass, battery_entry, MockLife360.apis[0], member)

        template = hass.states.get("sensor.issue_106_original_template")
        sensor = hass.states.get("sensor.life360_first_last_battery")
        tracker = hass.states.get("device_tracker.life360_first_last")
        assert template is not None and template.state == str(max(0, battery))
        assert sensor is not None
        assert sensor.state == (STATE_UNKNOWN if battery == -1 else str(battery))
        assert tracker is not None
        assert tracker.attributes["latitude"] == 40 + index / 100
        if battery == -1:
            assert ATTR_BATTERY_LEVEL not in tracker.attributes


@pytest.mark.parametrize(
    "battery",
    [None, "", "unknown", "NaN", "Infinity", "-Infinity", "101", "-0.5"],
)
async def test_invalid_battery_does_not_discard_location(
    hass: HomeAssistant,
    battery_entry: MockConfigEntry,
    MockLife360: MagicMock,
    battery: Any,
) -> None:
    """An unusable battery field should not break otherwise valid member data."""
    member = deepcopy(MEMBER)
    member["location"]["battery"] = battery
    member["location"]["latitude"] = "40.123"
    member["location"]["timestamp"] = "1792497601"
    await update_member(hass, battery_entry, MockLife360.apis[0], member)

    sensor = hass.states.get("sensor.life360_first_last_battery")
    tracker = hass.states.get("device_tracker.life360_first_last")
    assert sensor is not None and sensor.state == STATE_UNKNOWN
    assert tracker is not None
    assert tracker.attributes["latitude"] == 40.123
    assert ATTR_BATTERY_LEVEL not in tracker.attributes


async def test_missing_battery_does_not_discard_location(
    hass: HomeAssistant, battery_entry: MockConfigEntry, MockLife360: MagicMock
) -> None:
    """A missing battery field should not break otherwise valid member data."""
    member = deepcopy(MEMBER)
    del member["location"]["battery"]
    member["location"]["latitude"] = "40.123"
    member["location"]["timestamp"] = "1792497601"
    await update_member(hass, battery_entry, MockLife360.apis[0], member)

    sensor = hass.states.get("sensor.life360_first_last_battery")
    tracker = hass.states.get("device_tracker.life360_first_last")
    assert sensor is not None and sensor.state == STATE_UNKNOWN
    assert tracker is not None
    assert tracker.attributes["latitude"] == 40.123


@pytest.mark.parametrize("start_unknown", [False, True])
async def test_optional_last_known_template(
    hass: HomeAssistant,
    battery_entry: MockConfigEntry,
    MockLife360: MagicMock,
    start_unknown: bool,
) -> None:
    """The example preserves an explicit last known value and recovers, including 0%."""
    if start_unknown:
        member = deepcopy(MEMBER)
        member["location"]["battery"] = "-1"
        await update_member(hass, battery_entry, MockLife360.apis[0], member)
    example = Path(__file__).parents[1] / "examples/life360_battery_last_known.yaml"
    config = await hass.async_add_executor_job(
        lambda: yaml.safe_load(example.read_text())
    )
    hass.set_state(CoreState.starting)
    assert await async_setup_component(hass, "template", config)
    await hass.async_block_till_done(wait_background_tasks=True)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_START)
    await hass.async_block_till_done(wait_background_tasks=True)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done(wait_background_tasks=True)
    hass.set_state(CoreState.running)
    helper = hass.states.get("sensor.life360_battery_last_known")
    assert helper is not None
    assert helper.state == (STATE_UNKNOWN if start_unknown else "60")

    previous_timestamp = helper.attributes.get("last_valid_update")
    for index, (battery, expected) in enumerate(
        (
            (65, "65"),
            (60, "60"),
            (-1, "60"),
            (-1, "60"),
            (55, "55"),
            (0, "0"),
            (-1, "0"),
        )
    ):
        member = deepcopy(MEMBER)
        member["location"]["battery"] = str(battery)
        member["location"]["timestamp"] = str(1792497600 + index)
        await update_member(hass, battery_entry, MockLife360.apis[0], member)
        helper = hass.states.get("sensor.life360_battery_last_known")
        assert helper is not None and helper.state == expected
        timestamp = helper.attributes["last_valid_update"]
        if battery == -1:
            assert timestamp == previous_timestamp
        previous_timestamp = timestamp

    # A source outage must also leave the helper's last reading and timestamp intact.
    hass.states.async_set("sensor.life360_first_last_battery", STATE_UNAVAILABLE)
    await hass.async_block_till_done(wait_background_tasks=True)
    helper = hass.states.get("sensor.life360_battery_last_known")
    assert helper is not None and helper.state == "0"
    assert helper.attributes["last_valid_update"] == previous_timestamp


async def test_last_known_template_restores_during_gap(hass: HomeAssistant) -> None:
    """A restart during an upstream gap preserves the saved value and its age."""
    timestamp = "2026-10-20T12:00:00+00:00"
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(
                    "sensor.life360_battery_last_known",
                    "57",
                    {"last_valid_update": timestamp},
                ),
                {"native_value": 57, "native_unit_of_measurement": "%"},
            )
        ],
    )
    hass.states.async_set("sensor.life360_first_last_battery", STATE_UNKNOWN)
    example = Path(__file__).parents[1] / "examples/life360_battery_last_known.yaml"
    config = await hass.async_add_executor_job(
        lambda: yaml.safe_load(example.read_text())
    )
    assert await async_setup_component(hass, "template", config)
    await hass.async_block_till_done(wait_background_tasks=True)
    helper = hass.states.get("sensor.life360_battery_last_known")
    assert helper is not None and helper.state == "57"
    assert helper.attributes["last_valid_update"] == timestamp
