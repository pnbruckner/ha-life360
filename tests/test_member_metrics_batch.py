"""Batch regression tests for Life360 per-member metrics."""

import asyncio
from copy import deepcopy
from functools import partial
from itertools import repeat

import pytest
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    assert_setup_component,
)

from custom_components.life360.binary_sensor import MEMBER_SENSORS
from custom_components.life360.config_flow import Life360ConfigFlow
from custom_components.life360.const import DOMAIN
from custom_components.life360.helpers import ConfigOptions
from custom_components.life360.sensor import SENSORS

from .test_init import (
    cfg_options,
    cir1,
    get_circle_member,
    mem1,
    mem2,
)
from .test_member_binary_metrics import make_member_data

SENSOR_KEYS = (
    "bat_lvl",
    "spd",
    "gps_acc",
    "addr",
    "plc",
    "loc_since",
    "last_seen",
)
BINARY_KEYS = ("drv", "bat_chg", "wifi_on")

SENSOR_MAP = {description.key: description for description in SENSORS}
BINARY_MAP = {description.key: description for description in MEMBER_SENSORS}


def test_description_identity():
    """Every entity has a distinct, consistent abbreviated key."""
    assert tuple(SENSOR_MAP) == SENSOR_KEYS
    assert tuple(BINARY_MAP) == BINARY_KEYS

    for description in (*SENSORS, *MEMBER_SENSORS):
        assert description.translation_key == description.key


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("bat_lvl", 75.0),
        ("spd", 22.0),
        ("gps_acc", 10.0),
        ("addr", "Test address"),
        ("plc", "Home"),
    ],
)
def test_sensor_values(key, expected):
    """Verify all ordinary sensor values."""
    data = make_member_data()
    assert SENSOR_MAP[key].value_fn(data) == expected


@pytest.mark.parametrize("key", ("loc_since", "last_seen"))
def test_timestamp_values(key):
    """Timestamp sensors retain their timezone-aware datetimes."""
    data = make_member_data()
    expected = getattr(data.loc.details, "at_loc_since" if key == "loc_since" else key)

    assert SENSOR_MAP[key].value_fn(data) == expected
    assert expected.tzinfo is not None


@pytest.mark.parametrize("battery", (0.0, 100.0))
def test_battery_boundaries(battery):
    """Zero and full battery are valid values."""
    data = make_member_data()
    data.loc.battery_level = battery

    assert SENSOR_MAP["bat_lvl"].value_fn(data) == battery


def test_battery_not_reported():
    """Missing battery data must not become zero."""
    data = make_member_data()
    data.loc.battery_level = None

    assert SENSOR_MAP["bat_lvl"].value_fn(data) is None
    assert SENSOR_MAP["spd"].value_fn(data) == 22.0


@pytest.mark.parametrize(
    ("place", "expected"),
    [
        ("Home", "Home"),
        (["Home", "School"], "Home, School"),
        (None, None),
    ],
)
def test_place_forms(place, expected):
    """Handle single, multiple, and missing place names."""
    data = make_member_data()
    data.loc.details.place = place

    assert SENSOR_MAP["plc"].value_fn(data) == expected


def test_missing_address_and_zero_metrics():
    """Distinguish missing address from valid zero readings."""
    data = make_member_data()
    data.loc.details.address = None
    data.loc.details.speed = 0.0
    data.loc.details.gps_accuracy = 0.0

    assert SENSOR_MAP["addr"].value_fn(data) is None
    assert SENSOR_MAP["spd"].value_fn(data) == 0.0
    assert SENSOR_MAP["gps_acc"].value_fn(data) == 0.0


@pytest.mark.parametrize(
    ("driving", "threshold", "expected"),
    [
        (False, None, False),
        (False, 22.0, True),
        (False, 22.01, False),
        (True, 30.0, True),
        (True, None, True),
    ],
)
def test_driving_boundaries(driving, threshold, expected):
    """Check threshold equality and Life360's driving flag."""
    data = make_member_data()
    data.loc.details.driving = driving

    result = BINARY_MAP["drv"].value_fn(data, ConfigOptions(driving_speed=threshold))
    assert result is expected


@pytest.mark.parametrize("key", SENSOR_KEYS)
def test_no_location_sensor_values(key):
    """No-location sensor values must be missing."""
    data = make_member_data()
    data.loc = None

    assert SENSOR_MAP[key].value_fn(data) is None


@pytest.mark.parametrize("key", BINARY_KEYS)
def test_no_location_binary_values(key):
    """No-location binary readings must be unknown, not false."""
    data = make_member_data()
    data.loc = None

    assert BINARY_MAP[key].value_fn(data, ConfigOptions()) is None


async def settle(hass):
    """Allow membership dispatcher and entity setup tasks to finish."""
    await hass.async_block_till_done()
    await asyncio.sleep(0.1)
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    "MockLife360",
    [
        {
            "aid1": {
                "get_circles": repeat([cir1]),
                "get_circle_members": repeat([mem1, mem2]),
                "get_circle_member": partial(
                    get_circle_member,
                    {
                        cir1["id"]: {
                            mem1["id"]: mem1,
                            mem2["id"]: mem2,
                        }
                    },
                ),
            }
        }
    ],
    indirect=True,
)
async def test_member_lifecycle_and_devices(hass, entity_registry, MockLife360):
    """Check devices, removal, reappearance, and API efficiency."""

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=Life360ConfigFlow.VERSION,
        options=cfg_options(1, verbosity=3),
    )
    entry.add_to_hass(hass)

    with assert_setup_component(0, DOMAIN):
        assert await async_setup_component(hass, DOMAIN, {})
        await settle(hass)

    def entity_id(platform, mid, key):
        unique_id = f"{mid}-{key}" if key else mid
        return entity_registry.async_get_entity_id(platform, DOMAIN, unique_id)

    original_ids = {}
    original_devices = {}

    # Both members must have exactly ten metric entities,
    # grouped with their corresponding device trackers.
    for mid in ("mid1", "mid2"):
        tracker_id = entity_id("device_tracker", mid, None)
        assert tracker_id is not None

        device_id = entity_registry.async_get(tracker_id).device_id
        assert device_id is not None
        original_devices[mid] = device_id

        for platform, keys in (
            ("sensor", SENSOR_KEYS),
            ("binary_sensor", BINARY_KEYS),
        ):
            for key in keys:
                eid = entity_id(platform, mid, key)
                assert eid is not None, (mid, platform, key)
                assert hass.states.get(eid) is not None
                assert entity_registry.async_get(eid).device_id == device_id

                original_ids[(mid, platform, key)] = eid

    assert original_devices["mid1"] != original_devices["mid2"]

    # Reading metrics and supplying existing coordinator data
    # must not trigger new Life360 API requests.
    api = MockLife360.apis[0]
    before = (
        api.get_circles.call_count,
        api.get_circle_members.call_count,
        api.get_circle_member.call_count,
    )

    data = make_member_data()
    data.details.name = "First1 Last1"

    entry.runtime_data.mem_coordinator["mid1"].async_set_updated_data(data)
    await hass.async_block_till_done()

    assert float(
        hass.states.get(entity_id("sensor", "mid1", "bat_lvl")).state
    ) == pytest.approx(75.0)

    for eid in original_ids.values():
        assert hass.states.get(eid) is not None

    after = (
        api.get_circles.call_count,
        api.get_circle_members.call_count,
        api.get_circle_member.call_count,
    )
    assert after == before, "Metrics caused unexpected API calls"

    # Remove member 2 via the official circles/members coordinator.
    circles_coordinator = entry.runtime_data.coordinator
    original_data = deepcopy(circles_coordinator.data)
    one_member = deepcopy(original_data)

    one_member.mem_details.pop("mid2")
    for circle in one_member.circles.values():
        circle.mids.discard("mid2")

    circles_coordinator.async_set_updated_data(one_member)
    await settle(hass)

    assert "mid2" not in entry.runtime_data.mem_coordinator
    assert "mid1" in entry.runtime_data.mem_coordinator

    for (mid, platform, key), eid in original_ids.items():
        if mid == "mid2":
            removed_state = hass.states.get(eid)

            # HA may preserve the registry identity as a restored,
            # unavailable state after the actual entity is removed.
            assert removed_state is None or (
                removed_state.state == "unavailable"
                and removed_state.attributes.get("restored") is True
            ), (eid, removed_state)

            # Registry identity should remain reserved.
            assert entity_id(platform, mid, key) == eid
        else:
            assert hass.states.get(eid) is not None, eid

    # Reintroduce member 2 through that same coordinator.
    circles_coordinator.async_set_updated_data(original_data)
    await settle(hass)

    assert "mid2" in entry.runtime_data.mem_coordinator

    for (mid, platform, key), eid in original_ids.items():
        assert entity_id(platform, mid, key) == eid

        returned_state = hass.states.get(eid)
        assert returned_state is not None, eid

        # A restored placeholder is NOT a successful reappearance.
        assert returned_state.state != "unavailable", (eid, returned_state)
        assert returned_state.attributes.get("restored") is not True, eid

        assert entity_registry.async_get(eid).device_id == (original_devices[mid])

    # No duplicated registry entries for any member metric.
    for (mid, platform, key), eid in original_ids.items():
        matches = [
            item
            for item in entity_registry.entities.values()
            if item.platform == DOMAIN
            and item.domain == platform
            and item.unique_id == f"{mid}-{key}"
        ]
        assert len(matches) == 1, (mid, platform, key, matches)
        assert matches[0].entity_id == eid
