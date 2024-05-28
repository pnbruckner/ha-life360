"""Support for Life360 device tracking."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
from copy import deepcopy
from functools import cached_property
import logging
from typing import Any, cast

from homeassistant.components.device_tracker import SourceType
from homeassistant.components.device_tracker.config_entry import TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_BATTERY_CHARGING,
    ATTR_GPS_ACCURACY,
    STATE_NOT_HOME,
    STATE_UNKNOWN,
    UnitOfSpeed,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
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
    SIGNAL_MEMBERS_CHANGED,
    SIGNAL_UPDATE_LOCATION,
    STATE_DRIVING,
)
from .coordinator import (
    CirclesMembersDataUpdateCoordinator,
    MemberDataUpdateCoordinator,
)
from .helpers import ConfigOptions, MemberData, MemberID, NoLocReason

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the device tracker platform."""
    coordinator = cast(
        CirclesMembersDataUpdateCoordinator, hass.data[DOMAIN]["coordinator"]
    )
    mem_coordinator = cast(
        dict[MemberID, MemberDataUpdateCoordinator],
        hass.data[DOMAIN]["mem_coordinator"],
    )
    entities: dict[MemberID, Life360DeviceTracker] = {}

    async def async_process_data() -> None:
        """Process Members."""
        mids = set(coordinator.data.mem_details)
        cur_mids = set(entities)
        del_mids = cur_mids - mids
        add_mids = mids - cur_mids

        if del_mids:
            old_entities: list[Life360DeviceTracker] = []
            names: list[str] = []
            for mid in del_mids:
                entity = entities.pop(mid)
                old_entities.append(entity)
                names.append(str(entity))
            _LOGGER.debug("Deleting entities: %s", ", ".join(names))
            await asyncio.gather(*(entity.async_remove() for entity in old_entities))

        if add_mids:
            new_entities: list[Life360DeviceTracker] = []
            names = []
            for mid in add_mids:
                entity = Life360DeviceTracker(mem_coordinator[mid], mid)
                entities[mid] = entity
                new_entities.append(entity)
                names.append(str(entity))
            _LOGGER.debug("Adding entities: %s", ", ".join(names))
            async_add_entities(new_entities)

    async def update_location(entity_id: str | list[str]) -> None:
        """Request Member location update."""
        await asyncio.gather(
            *(
                entity.update_location()
                for entity in entities.values()
                if entity_id == "all" or entity.entity_id in entity_id
            )
        )

    await async_process_data()
    async_dispatcher_connect(hass, SIGNAL_MEMBERS_CHANGED, async_process_data)
    async_dispatcher_connect(hass, SIGNAL_UPDATE_LOCATION, update_location)


class Life360DeviceTracker(
    CoordinatorEntity[MemberDataUpdateCoordinator], TrackerEntity, RestoreEntity
):
    """Life360 Device Tracker."""

    _attr_attribution = ATTRIBUTION
    _attr_translation_key = "tracker"
    _attr_unique_id: MemberID
    coordinator: MemberDataUpdateCoordinator
    _warned_loc_unknown = False

    _unrecorded_attributes = frozenset(
        {
            ATTR_ADDRESS,
            ATTR_PLACE,
        }
    )

    def __init__(self, coordinator: MemberDataUpdateCoordinator, mid: MemberID) -> None:
        """Initialize Life360 Entity."""
        super().__init__(coordinator)
        self._attr_unique_id = mid
        self._options = ConfigOptions.from_dict(coordinator.config_entry.options)
        self._prev_data = self._data = deepcopy(coordinator.data)
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
            or self._data.details.name
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

        state = super().state
        if state == STATE_NOT_HOME and self._options.driving and self.driving:
            return STATE_DRIVING
        return state

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Return entity specific state attributes."""
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

        reason = {
            NoLocReason.NOT_SET: "Member data could not be retrieved",
            NoLocReason.NOT_SHARING: "Member is not sharing location",
        }.get(self._data.loc_missing, cast(str, self._data.err_msg))

        if not self._warned_loc_unknown:
            self._warned_loc_unknown = True
            _LOGGER.warning("Location data for %s is missing: %s", self, reason)

        return {ATTR_REASON: reason}

    @property
    def extra_restore_state_data(self) -> MemberData:
        """Return Life360 specific state data to be restored."""
        return self._data

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""
        await super().async_added_to_hass()

        # Restore state if possible.
        if not (last_extra_data := await self.async_get_last_extra_data()):
            return

        last_md = MemberData.from_dict(last_extra_data.as_dict())
        # Address data can be very old. Throw it away so it's not combined with
        # current address data.
        if last_md.loc:
            last_md.loc.details.address = None
        # If no data was actually available for Member (and MemberData was created just
        # based on MemberDetails, either from .storage/life360, or from initial query of
        # Circle Members), then replace current data with restored data.
        if not self._data.loc and self._data.loc_missing is NoLocReason.NOT_SET:
            self._prev_data = self._data = last_md
            return
        self._prev_data = last_md
        self._process_update()

    async def update_location(self) -> None:
        """Request Member location update.

        Typically causes the Member to update every 5 seconds for one minute.
        """
        # Ignore if the entity is disabled
        if not self.enabled:
            return
        await self.coordinator.update_location()

    @callback
    def _handle_coordinator_update(self, config_changed: bool = False) -> None:
        """Handle updated data from the coordinator."""
        if self.coordinator.data == self._data and not config_changed:
            return

        # Since _process_update might overwrite parts of the Member data (e.g., if
        # gps_accuracy is bad), and since the original data needs to be re-processed
        # when a config option changes (e.g., GPS accuracy limit), make a copy of
        # the data before processing it.
        self._data = deepcopy(self.coordinator.data)
        self._update_basic_attrs()
        self._process_update()

        super()._handle_coordinator_update()

    def _update_basic_attrs(self) -> None:
        """Update basic attributes."""
        self._attr_name = f"Life360 {self._data.details.name}"
        self._attr_entity_picture = self._data.details.entity_picture

    def _process_update(self) -> None:
        """Process new Member data."""
        if not self._data.loc or not self._prev_data.loc:
            self._prev_data = self._data
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
            # Overwrite new location details with previous values.
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

        self._prev_data = self._data

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
