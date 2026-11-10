"""Life360 Sensors."""

from abc import ABC, abstractmethod
import asyncio
from typing import Any, override

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfRatio
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION, SIGNAL_MEMBERS_CHANGED
from .coordinator import L360ConfigEntry, MemberDataUpdateCoordinator
from .helpers import MemberData, MemberID


async def async_setup_entry(
    hass: HomeAssistant,
    entry: L360ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform."""
    coordinator = entry.runtime_data.coordinator
    mem_coordinator = entry.runtime_data.mem_coordinator
    entities: dict[MemberID, Life360Sensor] = {}

    async def async_process_data() -> None:
        """Process Members."""
        mids = set(coordinator.data.mem_details)
        cur_mids = set(entities)

        if del_mids := cur_mids - mids:
            old_entities = [entities.pop(mid) for mid in del_mids]
            await asyncio.gather(
                *(entity.async_remove() for entity in old_entities if entity.enabled)
            )

        if add_mids := mids - cur_mids:
            new_entities = {
                mid: Life360BatLvlSensor(mem_coordinator[mid], mid) for mid in add_mids
            }
            entities.update(new_entities)
            async_add_entities(new_entities.values())

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_MEMBERS_CHANGED, async_process_data)
    )


class Life360Sensor(CoordinatorEntity[MemberDataUpdateCoordinator], SensorEntity, ABC):
    """Life360 Sensor."""

    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True

    def __init__(self, coordinator: MemberDataUpdateCoordinator, mid: MemberID) -> None:
        """Initialize Life360 Sensor."""
        super().__init__(coordinator)

        self._attr_unique_id = f"{mid}-{self._attr_translation_key}"
        self._update_data()

    @callback
    @override
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_data()
        super()._handle_coordinator_update()

    def _update_data(self) -> None:
        """Update from data values."""
        data = self.coordinator.data
        self._attr_translation_placeholders = {"name": data.details.name}
        self._attr_native_value = self._native_value(data)

    @abstractmethod
    def _native_value(self, data: MemberData) -> Any | None:
        """Return native value."""


class Life360BatLvlSensor(Life360Sensor):
    """Life360 Battery Level Sensor."""

    _attr_translation_key = "bat_lvl"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfRatio.PERCENTAGE

    @override
    def _native_value(self, data: MemberData) -> Any | None:
        """Return native value."""
        if not (loc := data.loc):
            return None
        return loc.battery_level
