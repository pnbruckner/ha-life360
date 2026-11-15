"""Tests for Life360 per-member binary sensor values."""

from datetime import UTC, datetime

from custom_components.life360.binary_sensor import MEMBER_SENSORS
from custom_components.life360.helpers import (
    ConfigOptions,
    LocationData,
    LocationDetails,
    MemberData,
    MemberDetails,
)


def make_member_data():
    """Create member data without making API requests."""
    now = datetime(2026, 11, 14, tzinfo=UTC)

    return MemberData(
        details=MemberDetails(name="Test Member"),
        loc=LocationData(
            details=LocationDetails(
                address="Test address",
                at_loc_since=now,
                driving=False,
                gps_accuracy=10.0,
                last_seen=now,
                latitude=33.9,
                longitude=-84.5,
                place="Home",
                speed=22.0,
            ),
            battery_charging=True,
            battery_level=75.0,
            wifi_on=False,
        ),
    )


def test_binary_sensor_values():
    data = make_member_data()
    sensors = {desc.key: desc for desc in MEMBER_SENSORS}
    options = ConfigOptions()

    assert sensors["drv"].value_fn(data, options) is False
    assert sensors["bat_chg"].value_fn(data, options) is True
    assert sensors["wifi_on"].value_fn(data, options) is False


def test_driving_speed_threshold():
    data = make_member_data()
    driving = next(desc for desc in MEMBER_SENSORS if desc.key == "drv")

    # Reported speed is 22 mph; Life360 driving flag is false.
    assert driving.value_fn(data, ConfigOptions(driving_speed=25.0)) is False

    # Lowering the threshold changes the calculated result.
    assert driving.value_fn(data, ConfigOptions(driving_speed=20.0)) is True

    # The original Life360 driving flag still works.
    data.loc.details.driving = True
    assert driving.value_fn(data, ConfigOptions(driving_speed=25.0)) is True


def test_missing_location():
    data = MemberData(details=MemberDetails(name="Test Member"))
    options = ConfigOptions()

    for description in MEMBER_SENSORS:
        assert description.value_fn(data, options) is None
