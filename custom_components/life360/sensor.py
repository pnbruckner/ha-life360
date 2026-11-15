"""Life360 per-Member sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import override

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfLength, UnitOfRatio, UnitOfSpeed
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType

from .const import SPEED_DIGITS
from .coordinator import L360ConfigEntry, MemberDataUpdateCoordinator
from .entity import Life360MemberEntity, setup_member_entities
from .helpers import MemberData, MemberID


def _place(data: MemberData) -> str | None:
    """Return Place name(s) as a string."""
    if not data.loc:
        return None
    place = data.loc.details.place
    if isinstance(place, list):
        return ", ".join(place)
    return place


@dataclass(frozen=True, kw_only=True)
class Life360SensorDescription(SensorEntityDescription):
    """Life360 sensor description."""

    value_fn: Callable[[MemberData], StateType | datetime]


SENSORS: tuple[Life360SensorDescription, ...] = (
    Life360SensorDescription(
        key="bat_lvl",
        translation_key="bat_lvl",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=UnitOfRatio.PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.loc.battery_level if d.loc else None,
    ),
    Life360SensorDescription(
        key="spd",
        translation_key="spd",
        device_class=SensorDeviceClass.SPEED,
        # Life360 speed is converted to MPH by helpers.py. HA converts to the
        # user's unit system for display.
        native_unit_of_measurement=UnitOfSpeed.MILES_PER_HOUR,
        suggested_display_precision=SPEED_DIGITS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.loc.details.speed if d.loc else None,
    ),
    Life360SensorDescription(
        key="gps_acc",
        translation_key="gps_acc",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.METERS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.loc.details.gps_accuracy if d.loc else None,
    ),
    Life360SensorDescription(
        key="addr",
        translation_key="addr",
        value_fn=lambda d: d.loc.details.address if d.loc else None,
    ),
    Life360SensorDescription(
        key="plc",
        translation_key="plc",
        value_fn=_place,
    ),
    Life360SensorDescription(
        key="loc_since",
        translation_key="loc_since",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda d: d.loc.details.at_loc_since if d.loc else None,
    ),
    Life360SensorDescription(
        key="last_seen",
        translation_key="last_seen",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.loc.details.last_seen if d.loc else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: L360ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform."""
    setup_member_entities(
        hass,
        entry,
        async_add_entities,
        lambda crd, mid: [Life360Sensor(crd, mid, desc) for desc in SENSORS],
    )


class Life360Sensor(Life360MemberEntity, SensorEntity):
    """Life360 per-Member sensor."""

    entity_description: Life360SensorDescription

    def __init__(
        self,
        coordinator: MemberDataUpdateCoordinator,
        mid: MemberID,
        description: Life360SensorDescription,
    ) -> None:
        """Initialize sensor."""
        super().__init__(coordinator, mid, description.key)
        self.entity_description = description

    @property
    @override
    def native_value(self) -> StateType | datetime:
        """Return sensor value."""
        return self.entity_description.value_fn(self.member_data)
