"""Support for Life360 device tracking."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from copy import deepcopy
from datetime import datetime
from functools import partial
from typing import Any, cast

# SourceType was new in 2022.9
try:
    from homeassistant.components.device_tracker import SourceType

    source_type_type = SourceType
    source_type_gps = SourceType.GPS
except ImportError:
    from homeassistant.components.device_tracker import SOURCE_TYPE_GPS

    source_type_type = str  # type: ignore[assignment, misc]
    source_type_gps = SOURCE_TYPE_GPS  # type: ignore[assignment]
from homeassistant.components.device_tracker.config_entry import TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_BATTERY_CHARGING, ATTR_GPS_ACCURACY, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_ADDRESS,
    ATTR_AT_LOC_SINCE,
    ATTR_DRIVING,
    ATTR_IGNORED_UPDATE_REASONS,
    ATTR_LAST_SEEN,
    ATTR_PLACE,
    ATTR_REASON,
    ATTR_SPEED,
    ATTR_WIFI_ON,
    ATTRIBUTION,
    CONF_DRIVING_SPEED,
    CONF_MAX_GPS_ACCURACY,
    LOGGER,
    SHOW_DRIVING,
    STATE_DRIVING,
)
from .coordinator import (
    Life360DataUpdateCoordinator,
    Member,
    MemberID,
    MemberStatus,
    life360_central_coordinator,
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the device tracker platform."""
    coordinator = life360_central_coordinator(hass).config_coordinator(
        config_entry.entry_id
    )
    tracked_members: set[MemberID] = set()

    def remove_tracked_member(memberid: MemberID) -> None:
        """Remove a tracked member."""
        tracked_members.remove(memberid)

    def process_data() -> None:
        """Process new Life360 Member data."""
        if not coordinator.last_update_success:
            return

        if create_entities := set(coordinator.data) - tracked_members:
            new_entities: list[Life360DeviceTracker] = []
            for member_id in create_entities:
                entity = Life360DeviceTracker(coordinator, member_id)
                tracked_members.add(member_id)
                entity.async_on_remove(partial(remove_tracked_member, member_id))
                new_entities.append(entity)
            LOGGER.info("%s: add entities: %s", config_entry.title, new_entities)
            async_add_entities(new_entities)

    process_data()
    config_entry.async_on_unload(coordinator.async_add_listener(process_data))


class Life360DeviceTracker(
    CoordinatorEntity[Life360DataUpdateCoordinator], TrackerEntity
):
    """Life360 Device Tracker."""

    _attr_attribution = ATTRIBUTION
    _attr_unique_id: MemberID
    coordinator: Life360DataUpdateCoordinator
    _registry_entry_updated = False
    _warned_loc_unknown = False

    def __init__(
        self, coordinator: Life360DataUpdateCoordinator, member_id: MemberID
    ) -> None:
        """Initialize Life360 Entity."""
        super().__init__(coordinator)
        self._attr_unique_id = member_id

        self._options = coordinator.config_entry.options.copy()
        self._data: Member | None = deepcopy(coordinator.data[member_id])
        self._prev_data = self._data
        self._ignored_update_reasons: list[str] = []

        if self._data.status == MemberStatus.VALID:
            if (address := self._data.loc.address) == self._data.loc.place:
                address = None
            self._addresses: list[str | None] = [address]
        else:
            self._addresses = []

        self.async_on_remove(
            coordinator.config_entry.add_update_listener(
                self._async_config_entry_updated
            )
        )

    def __repr__(self) -> str:
        """Return identification string."""
        if name := (
            self.registry_entry
            and (self.registry_entry.name or self.registry_entry.original_name)
            or self._data
            and self._data.name
        ):
            return f"{name} ({self.entity_id})"
        return self.entity_id

    async def _async_config_entry_updated(
        self, hass: HomeAssistant, config_entry: ConfigEntry
    ) -> None:
        """Run when the config entry has been updated."""
        if self._options != (new_options := config_entry.options.copy()):
            self._options = new_options
            # Re-process current data.
            self._handle_coordinator_update()

    @callback
    def async_registry_entry_updated(self) -> None:
        """Run when the entity registry entry has been updated."""
        self._registry_entry_updated = True

    @callback
    def _async_registry_updated(self, event: Event) -> None:
        """Handle entity registry update."""
        super()._async_registry_updated(event)
        if self._registry_entry_updated:
            self._registry_entry_updated = False
            if "config_entry_id" in event.data["changes"]:
                # Member has been reassigned to a new config entry so this entity
                # needs to be removed before it is recreated by new config entry.
                # This will be handled by the system when the config entry is
                # unloaded.
                # However, config entry is still loaded and associated with the
                # coordinator that was used to create this entity. Therefore, the
                # entity needs to be removed here.
                self.hass.async_create_task(self.async_remove())

    def _process_update(self) -> None:
        """Process new Member data."""
        assert self._data

        # Check if we should effectively throw out new location data.
        last_seen = cast(datetime, self._data.loc.last_seen)
        prev_seen = cast(datetime, self._prev_data.loc.last_seen)
        max_gps_acc = self._options.get(CONF_MAX_GPS_ACCURACY)
        bad_last_seen = last_seen < prev_seen
        bad_accuracy = max_gps_acc is not None and self.location_accuracy > max_gps_acc

        if bad_last_seen or bad_accuracy:
            if bad_last_seen and ATTR_LAST_SEEN not in self._ignored_update_reasons:
                self._ignored_update_reasons.append(ATTR_LAST_SEEN)
                LOGGER.warning(
                    "%s: Ignoring location update because "
                    "last_seen (%s) < previous last_seen (%s)",
                    self,
                    last_seen,
                    prev_seen,
                )
            if bad_accuracy and ATTR_GPS_ACCURACY not in self._ignored_update_reasons:
                self._ignored_update_reasons.append(ATTR_GPS_ACCURACY)
                LOGGER.warning(
                    "%s: Ignoring location update because "
                    "expected GPS accuracy (%0.1f) is not met: %i",
                    self,
                    max_gps_acc,
                    self.location_accuracy,
                )
            # Overwrite new location related data with previous values.
            self._data.loc = self._prev_data.loc

        else:
            self._ignored_update_reasons.clear()

            if (address := self._data.loc.address) == self._data.loc.place:
                address = None
            if last_seen != prev_seen:
                if address not in self._addresses:
                    self._addresses = [address]
            elif self._data.loc.address != self._prev_data.loc.address:
                if address not in self._addresses:
                    if len(self._addresses) < 2:
                        self._addresses.append(address)
                    else:
                        self._addresses = [address]

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self.available:
            # Since _process_update might overwrite parts of the Member data (e.g., if
            # gps_accuracy is bad), and since the original data needs to be re-processed
            # when a config option changes (e.g., GPS accuracy limit), make a copy of
            # the data before processing it.
            # Note that it's possible that there is no data for this Member on some
            # updates (e.g., if a Member is no longer visible and this Entity is in the
            # process of being removed.)
            self._data = deepcopy(self.coordinator.data.get(self._attr_unique_id))
            if self._data:
                if (
                    self._data.status == MemberStatus.VALID
                    and self._prev_data.status == MemberStatus.VALID
                ):
                    self._process_update()
                # Keep copy of processed data used to update entity.
                # New server data will be compared against this on next update.
                self._prev_data = self._data
        else:
            self._data = None

        super()._handle_coordinator_update()

    @property
    def name(self) -> str | None:
        """Return the name of the entity."""
        if self._data:
            self._attr_name = self._data.name
        return self._attr_name

    @property
    def force_update(self) -> bool:
        """Return True if state updates should be forced.
        Overridden because CoordinatorEntity sets `should_poll` to False,
        which causes TrackerEntity to set `force_update` to True.
        """
        return False

    @property
    def entity_picture(self) -> str | None:
        """Return the entity picture to use in the frontend, if any."""
        if self._data:
            self._attr_entity_picture = self._data.entity_picture
        return self._attr_entity_picture

    @property
    def battery_level(self) -> int | None:
        """Return the battery level of the device.

        Percentage from 0-100.
        """
        if not self._data or self._data.status != MemberStatus.VALID:
            return None
        return self._data.battery_level

    @property
    def source_type(self) -> source_type_type:
        """Return the source type, eg gps or router, of the device."""
        return source_type_gps

    @property
    def location_accuracy(self) -> int:
        """Return the location accuracy of the device.

        Value in meters.
        """
        if not self._data or self._data.status != MemberStatus.VALID:
            return 0
        return cast(int, self._data.loc.gps_accuracy)

    @property
    def driving(self) -> bool:
        """Return if driving."""
        if not self._data or self._data.status != MemberStatus.VALID:
            return False
        if (driving_speed := self._options.get(CONF_DRIVING_SPEED)) is not None:
            if cast(float, self._data.loc.speed) >= driving_speed:
                return True
        return cast(bool, self._data.loc.driving)

    @property
    def state(self) -> str | None:
        """Return the state of the device."""
        # If update from server is available, but location details are missing, set
        # state to "unknown"; "reason" attribute will indicate why (e.g., Member is not
        # sharing location details, etc.)
        if not self._data or self._data.status != MemberStatus.VALID:
            return STATE_UNKNOWN

        if self._options.get(SHOW_DRIVING) and self.driving:
            return STATE_DRIVING
        return super().state

    @property
    def latitude(self) -> float | None:
        """Return latitude value of the device."""
        if not self._data or self._data.status != MemberStatus.VALID:
            return None
        return cast(float, self._data.loc.latitude)

    @property
    def longitude(self) -> float | None:
        """Return longitude value of the device."""
        if not self._data or self._data.status != MemberStatus.VALID:
            return None
        return cast(float, self._data.loc.longitude)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Return entity specific state attributes."""
        attrs_unknown = {
            ATTR_ADDRESS: None,
            ATTR_AT_LOC_SINCE: None,
            ATTR_BATTERY_CHARGING: None,
            ATTR_DRIVING: None,
            ATTR_LAST_SEEN: None,
            ATTR_PLACE: None,
            ATTR_SPEED: None,
            ATTR_WIFI_ON: None,
        }

        if not self._data:
            self._warned_loc_unknown = False
            return attrs_unknown

        if self._data.status == MemberStatus.VALID:
            self._warned_loc_unknown = False

            address1: str | None = None
            address2: str | None = None
            with suppress(IndexError):
                address1 = self._addresses[0]
                address2 = self._addresses[1]
            if address1 and address2:
                address: str | None = " / ".join(sorted([address1, address2]))
            else:
                address = address1 or address2

            attrs: dict[str, Any] = {
                ATTR_ADDRESS: address,
                ATTR_AT_LOC_SINCE: self._data.loc.at_loc_since,
                ATTR_BATTERY_CHARGING: self._data.battery_charging,
                ATTR_DRIVING: self.driving,
                ATTR_LAST_SEEN: self._data.loc.last_seen,
                ATTR_PLACE: self._data.loc.place,
                ATTR_SPEED: self._data.loc.speed,
                ATTR_WIFI_ON: self._data.wifi_on,
            }
            if self._ignored_update_reasons:
                attrs[ATTR_IGNORED_UPDATE_REASONS] = self._ignored_update_reasons
            return attrs

        if not self._warned_loc_unknown:
            self._warned_loc_unknown = True
            if self._data.status == MemberStatus.NOT_SHARING:
                LOGGER.warning("%s is not sharing location data", self)
            else:
                LOGGER.warning(
                    "Location data is missing for %s: %s", self, self._data.err_msg
                )

        if self._data.status == MemberStatus.NOT_SHARING:
            return attrs_unknown | {ATTR_REASON: "Member is not sharing location"}
        return attrs_unknown | {ATTR_REASON: self._data.err_msg}
