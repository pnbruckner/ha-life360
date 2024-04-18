"""Support for Life360 device tracking."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from copy import deepcopy
from functools import cached_property, partial
import logging
from typing import Any, cast

from homeassistant.components.device_tracker import SourceType
from homeassistant.components.device_tracker.config_entry import TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_BATTERY_CHARGING,
    ATTR_GPS_ACCURACY,
    STATE_UNKNOWN,
    UnitOfSpeed,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.unit_conversion import SpeedConverter
from homeassistant.util.unit_system import METRIC_SYSTEM

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
    DOMAIN,
    STATE_DRIVING,
)
from .coordinator import Life360DataUpdateCoordinator
from .helpers import ConfigOptions, MemberID, NoLocReason

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the device tracker platform."""
    coordinator = cast(Life360DataUpdateCoordinator, hass.data[DOMAIN])
    tracked_mids: set[MemberID] = set()

    def remove_tracked_mid(mid: MemberID) -> None:
        """Remove a tracked member."""
        tracked_mids.remove(mid)

    def process_data() -> None:
        """Process new Life360 Member data."""
        if not (new_mids := set(coordinator.data) - tracked_mids):
            return
        new_entities: list[Life360DeviceTracker] = []
        for mid in new_mids:
            entity = Life360DeviceTracker(coordinator, mid)
            tracked_mids.add(mid)
            entity.async_on_remove(partial(remove_tracked_mid, mid))
            new_entities.append(entity)
        _LOGGER.debug("add entities: %s", new_entities)
        async_add_entities(new_entities)

    process_data()
    entry.async_on_unload(coordinator.async_add_listener(process_data))


# TODO: Restore state
class Life360DeviceTracker(
    CoordinatorEntity[Life360DataUpdateCoordinator], TrackerEntity
):
    """Life360 Device Tracker."""

    _attr_attribution = ATTRIBUTION
    _attr_unique_id: MemberID
    coordinator: Life360DataUpdateCoordinator
    _warned_loc_unknown = False

    _unrecorded_attributes = frozenset(
        {
            ATTR_ADDRESS,
            ATTR_PLACE,
        }
    )

    def __init__(
        self, coordinator: Life360DataUpdateCoordinator, mid: MemberID
    ) -> None:
        """Initialize Life360 Entity."""
        super().__init__(coordinator)
        self._attr_unique_id = mid
        self._options = ConfigOptions.from_dict(coordinator.config_entry.options)
        self._prev_data = self._data = deepcopy(coordinator.data[mid])
        self._update_basic_attrs()
        self._ignored_update_reasons: list[str] = []

        if self._data.loc:
            address = self._data.loc.details.address
            if address == self._data.loc.details.place:
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
            or self._data.name
        ):
            return f"{name} ({self.entity_id})"
        return self.entity_id

    @cached_property
    def _mid(self) -> MemberID:
        """Return Member ID."""
        return self._attr_unique_id

    @property
    def _metric(self) -> bool:
        """Return if system is configured for Metric."""
        return self.hass.config.units is METRIC_SYSTEM

    @property
    def force_update(self) -> bool:
        """Return True if state updates should be forced.

        Overridden because CoordinatorEntity sets `should_poll` to False,
        which causes TrackerEntity to set `force_update` to True.
        """
        return False

    @property
    def battery_level(self) -> int | None:
        """Return the battery level of the device.

        Percentage from 0-100.
        """
        if not self._data.loc:
            return None
        return self._data.loc.battery_level

    @property
    def source_type(self) -> SourceType:
        """Return the source type, eg gps or router, of the device."""
        return SourceType.GPS

    @property
    def location_accuracy(self) -> int:
        """Return the location accuracy of the device.

        Value in meters.
        """
        if not self._data.loc:
            return 0
        return self._data.loc.details.gps_accuracy

    @property
    def latitude(self) -> float | None:
        """Return latitude value of the device."""
        if not self._data.loc:
            return None
        return self._data.loc.details.latitude

    @property
    def longitude(self) -> float | None:
        """Return longitude value of the device."""
        if not self._data.loc:
            return None
        return self._data.loc.details.longitude

    # TODO: When driving is True, periodically send update requests to server for
    #       Member, maybe once a minute??? But only if enabled by config option.
    @property
    def driving(self) -> bool:
        """Return if driving."""
        if not self._data.loc:
            return False
        if (driving_speed := self._options.driving_speed) is not None:
            if self._data.loc.details.speed >= driving_speed:
                return True
        return self._data.loc.details.driving

    @property
    def state(self) -> str | None:
        """Return the state of the device."""
        # If location details are missing, set state to "unknown"; "reason" attribute
        # will indicate why (e.g., Member is not sharing location details, etc.)
        if not self._data.loc:
            return STATE_UNKNOWN

        if self._options.driving and self.driving:
            return STATE_DRIVING
        return super().state

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

        if self._data.loc:
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

            # Speed is returned in MPH. Convert to KPH if system configured for Metric.
            speed = self._data.loc.details.speed
            if self._metric:
                speed = SpeedConverter.convert(
                    speed,
                    UnitOfSpeed.MILES_PER_HOUR,
                    UnitOfSpeed.KILOMETERS_PER_HOUR,
                )

            attrs: dict[str, Any] = {
                ATTR_ADDRESS: address,
                ATTR_AT_LOC_SINCE: self._data.loc.details.at_loc_since,
                ATTR_BATTERY_CHARGING: self._data.loc.battery_charging,
                ATTR_DRIVING: self.driving,
                ATTR_LAST_SEEN: self._data.loc.details.last_seen,
                ATTR_PLACE: self._data.loc.details.place,
                ATTR_SPEED: speed,
                ATTR_WIFI_ON: self._data.loc.wifi_on,
            }
            if self._ignored_update_reasons:
                attrs[ATTR_IGNORED_UPDATE_REASONS] = self._ignored_update_reasons
            return attrs

        if not self._warned_loc_unknown:
            self._warned_loc_unknown = True
            _LOGGER.warning(
                "Location data for %s is missing; see %s attribute for more details",
                self,
                ATTR_REASON,
            )

        if self._data.loc_missing is NoLocReason.NOT_SHARING:
            return attrs_unknown | {ATTR_REASON: "Member is not sharing location"}
        return attrs_unknown | {ATTR_REASON: self._data.err_msg}

    @callback
    def _handle_coordinator_update(self, config_changed: bool = False) -> None:
        """Handle updated data from the coordinator."""
        latest_data = self.coordinator.data[self._mid]
        if latest_data == self._data and not config_changed:
            return

        # Since _process_update might overwrite parts of the Member data (e.g., if
        # gps_accuracy is bad), and since the original data needs to be re-processed
        # when a config option changes (e.g., GPS accuracy limit), make a copy of
        # the data before processing it.
        self._data = deepcopy(latest_data)
        self._update_basic_attrs()
        self._process_update()
        # Keep copy of processed data used to update entity.
        # New server data will be compared against this on next update.
        self._prev_data = self._data

        super()._handle_coordinator_update()

    def _update_basic_attrs(self) -> None:
        """Update basic attributes."""
        self._attr_name = f"Life360 {self._data.name}"
        self._attr_entity_picture = self._data.entity_picture

    def _process_update(self) -> None:
        """Process new Member data."""
        if not self._data.loc or not self._prev_data.loc:
            return

        # Check if we should effectively throw out new location data.
        last_seen = self._data.loc.details.last_seen
        prev_seen = self._prev_data.loc.details.last_seen
        max_gps_acc = self._options.max_gps_accuracy
        bad_last_seen = last_seen < prev_seen
        bad_accuracy = max_gps_acc is not None and self.location_accuracy > max_gps_acc

        if bad_last_seen or bad_accuracy:
            if bad_last_seen and ATTR_LAST_SEEN not in self._ignored_update_reasons:
                self._ignored_update_reasons.append(ATTR_LAST_SEEN)
                _LOGGER.warning(
                    "%s: Ignoring location update because "
                    "last_seen (%s) < previous last_seen (%s)",
                    self,
                    last_seen,
                    prev_seen,
                )
            if bad_accuracy and ATTR_GPS_ACCURACY not in self._ignored_update_reasons:
                self._ignored_update_reasons.append(ATTR_GPS_ACCURACY)
                _LOGGER.warning(
                    "%s: Ignoring location update because "
                    "expected GPS accuracy (%0.1f) is not met: %i",
                    self,
                    max_gps_acc,
                    self.location_accuracy,
                )
            # Overwrite new location related data with previous values.
            self._data.loc.details = self._prev_data.loc.details

        else:
            self._ignored_update_reasons.clear()

            if (
                address := self._data.loc.details.address
            ) == self._data.loc.details.place:
                address = None
            if last_seen != prev_seen:
                if address not in self._addresses:
                    self._addresses = [address]
            elif self._data.loc.details.address != self._prev_data.loc.details.address:
                if address not in self._addresses:
                    if len(self._addresses) < 2:
                        self._addresses.append(address)
                    else:
                        self._addresses = [address]

    async def _async_config_entry_updated(
        self, _: HomeAssistant, entry: ConfigEntry
    ) -> None:
        """Run when the config entry has been updated."""
        if self._options == (new_options := ConfigOptions.from_dict(entry.options)):
            return

        old_options = self._options
        self._options = new_options

        need_to_reprocess = any(
            getattr(old_options, attr) != getattr(new_options, attr)
            for attr in ("driving", "driving_speed", "max_gps_accuracy")
        )
        if not need_to_reprocess:
            return

        # Re-process current data.
        self._handle_coordinator_update(config_changed=True)
