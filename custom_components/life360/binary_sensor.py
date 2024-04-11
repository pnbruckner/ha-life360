"""Life360 Binary Sensor."""

# from __future__ import annotations

# from collections.abc import Mapping
# from typing import Any

# from homeassistant.components.binary_sensor import (
#     BinarySensorDeviceClass,
#     BinarySensorEntity,
# )
# from homeassistant.config_entries import ConfigEntry
# from homeassistant.core import HomeAssistant, callback
# from homeassistant.exceptions import ConfigEntryAuthFailed
# from homeassistant.helpers.entity_platform import AddEntitiesCallback
# from homeassistant.helpers.update_coordinator import CoordinatorEntity

# from .const import ATTR_REASON
# from .coordinator import Life360DataUpdateCoordinator, life360_central_coordinator


# async def async_setup_entry(
#     hass: HomeAssistant,
#     config_entry: ConfigEntry,
#     async_add_entities: AddEntitiesCallback,
# ) -> None:
#     """Set up the binary sensory platform."""
#     coordinator = life360_central_coordinator(hass).config_coordinator(
#         config_entry.entry_id
#     )
#     async_add_entities([Life360BinarySensor(coordinator)])


# class Life360BinarySensor(
#     CoordinatorEntity[Life360DataUpdateCoordinator], BinarySensorEntity
# ):
#     """Life360 Binary Sensor."""

#     _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

#     def __init__(self, coordinator: Life360DataUpdateCoordinator) -> None:
#         """Initialize binary sensor."""
#         unique_id = coordinator.config_entry.unique_id
#         self._attr_name = f"life360 online ({unique_id})"
#         self._attr_unique_id = unique_id
#         super().__init__(coordinator)
#         self._attr_is_on = self.is_online

#     @property
#     def is_online(self) -> bool:
#         """Return if config/account is online."""
#         return super().available

#     @property
#     def available(self) -> bool:
#         """Return if entity is available."""
#         return True

#     @callback
#     def _handle_coordinator_update(self) -> None:
#         """Handle updated data from the coordinator."""
#         self._attr_is_on = self.is_online
#         super()._handle_coordinator_update()

#     @property
#     def extra_state_attributes(self) -> Mapping[str, Any] | None:
#         """Return entity specific state attributes."""
#         if self.is_online:
#             return None

#         if isinstance(self.coordinator.last_exception, ConfigEntryAuthFailed):
#             return {ATTR_REASON: "Login error"}
#         return {ATTR_REASON: "Server communication error"}
