"""Life360 battery sensors."""

from __future__ import annotations

import asyncio
from typing import override

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION, SIGNAL_MEMBERS_CHANGED
from .coordinator import L360ConfigEntry, MemberDataUpdateCoordinator
from .helpers import MemberID


async def async_setup_entry(
    hass: HomeAssistant,
    entry: L360ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up battery sensors as members are discovered."""
    entities: dict[MemberID, Life360BatterySensor] = {}

    async def async_process_data() -> None:
        """Add and remove sensors for the current members."""
        mids = set(entry.runtime_data.coordinator.data.mem_details)
        cur_mids = set(entities)
        old_entities = [entities.pop(mid) for mid in cur_mids - mids]
        await asyncio.gather(
            *(entity.async_remove() for entity in old_entities if entity.enabled)
        )

        new_entities = {
            mid: Life360BatterySensor(entry.runtime_data.mem_coordinator[mid], mid)
            for mid in mids - cur_mids
        }
        entities.update(new_entities)
        if new_entities:
            async_add_entities(new_entities.values())

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_MEMBERS_CHANGED, async_process_data)
    )


class Life360BatterySensor(
    CoordinatorEntity[MemberDataUpdateCoordinator], SensorEntity
):
    """Battery level reported by Life360 for a member."""

    _attr_attribution = ATTRIBUTION
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: MemberDataUpdateCoordinator, mid: MemberID) -> None:
        """Initialize the battery sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{mid}_battery"
        self._update_basic_attrs()

    @property
    @override
    def native_value(self) -> int | None:
        """Return the battery percentage, or None if it is unknown."""
        if not (loc := self.coordinator.data.loc):
            return None
        return loc.battery_level

    @callback
    @override
    def _handle_coordinator_update(self) -> None:
        """Update member details before writing the new state."""
        self._update_basic_attrs()
        super()._handle_coordinator_update()

    def _update_basic_attrs(self) -> None:
        """Update the member name and picture."""
        self._attr_name = f"Life360 {self.coordinator.data.details.name} battery"
        self._attr_entity_picture = self.coordinator.data.details.entity_picture
