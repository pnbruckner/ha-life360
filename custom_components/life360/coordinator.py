"""DataUpdateCoordinator for the Life360 integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from itertools import groupby
from typing import Any, NewType, cast

from life360 import Life360, Life360Error, LoginError

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import (
    EVENT_COMPONENT_LOADED,
    LENGTH_FEET,
    LENGTH_KILOMETERS,
    LENGTH_METERS,
    LENGTH_MILES,
    Platform,
)
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import entity_registry
from homeassistant.helpers.entity_registry import RegistryEntry, RegistryEntryDisabler
from homeassistant.helpers.typing import UNDEFINED, UndefinedType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.setup import ATTR_COMPONENT
from homeassistant.util.distance import convert
import homeassistant.util.dt as dt_util

from .const import (
    COMM_MAX_RETRIES,
    COMM_TIMEOUT,
    CONF_AUTHORIZATION,
    DATA_CENTRAL_COORDINATOR,
    DOMAIN,
    LOGGER,
    SPEED_DIGITS,
    SPEED_FACTOR_MPH,
    UPDATE_INTERVAL,
)


def init_life360_coordinator(hass: HomeAssistant) -> None:
    """Initialize module."""
    hass.data[DOMAIN][DATA_CENTRAL_COORDINATOR] = Life360CentralDataUpdateCoordinator(
        hass
    )


async def async_unloading_life360_config_entry(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Config entry is being unloaded."""
    await cast(
        Life360CentralDataUpdateCoordinator, hass.data[DOMAIN][DATA_CENTRAL_COORDINATOR]
    ).async_unloading_config(entry)


@dataclass
class Place:
    """Life360 Place data."""

    name: str
    latitude: float
    longitude: float
    radius: float


PlaceID = NewType("PlaceID", str)
Places = dict[PlaceID, Place]


@dataclass
class Circle:
    """Life360 Circle data."""

    name: str
    places: Places
    # ID of ConfigEntry whose associated account was used to fetch the Circle's data
    cfg_id: str


CircleID = NewType("CircleID", str)


class MemberStatus(IntEnum):
    """Status of dynamic member data."""

    VALID = 3
    MISSING_W_REASON = 2
    MISSING_NO_REASON = 1
    NOT_SHARING = 0


@dataclass
class MemberLocation:
    """Life360 Member location data."""

    address: str | None = None
    at_loc_since: datetime | None = None
    driving: bool | None = None
    gps_accuracy: int | None = None
    last_seen: datetime | None = None
    latitude: float | None = None
    longitude: float | None = None
    place: str | None = None
    speed: float | None = None


@dataclass
class Member:
    """Life360 Member data."""

    name: str
    entity_picture: str | None

    # status applies to the following fields
    loc: MemberLocation = field(default_factory=MemberLocation)
    battery_charging: bool | None = None
    battery_level: int | None = None
    wifi_on: bool | None = None

    status: MemberStatus = MemberStatus.VALID
    err_msg: str | None = field(default=None, compare=False)

    # Since a Member can exist in more than one Circle, and the data retrieved for the
    # Member might be different in each (e.g., some might not share location info but
    # others do), provide a means to find the "best" data for the Member from a list of
    # data, one from each Circle.
    def __lt__(self, other: Member) -> bool:
        """Determine if this member should sort before another."""
        if self.status < other.status:
            return True
        if not (self.status == other.status == MemberStatus.VALID):
            return False
        if not self.loc.place and other.loc.place:
            return True
        return cast(datetime, self.loc.last_seen) < cast(datetime, other.loc.last_seen)


MemberID = NewType("MemberID", str)
Members = dict[MemberID, Member]


class Life360DataUpdateCoordinator(DataUpdateCoordinator[Members]):
    """Life360 config entry data update coordinator."""

    config_entry: ConfigEntry
    data: Members
    _update = False

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize data update coordinator."""
        self._central_coordinator = cast(
            Life360CentralDataUpdateCoordinator,
            hass.data[DOMAIN][DATA_CENTRAL_COORDINATOR],
        )

        # No periodic updates. Central coordinator will provide manual updates.
        super().__init__(hass, LOGGER, name="", update_method=self._async_first_refresh)
        self.name = self.config_entry.title
        self.config_entry.async_on_unload(
            self.config_entry.add_update_listener(self._async_cfg_entry_updated)
        )

    async def async_request_refresh(self) -> None:
        """Request a refresh."""
        await self._central_coordinator.async_request_refresh()

    @property
    def update(self) -> bool:
        """Return if data should be updated by central coordinator."""
        return self._update

    @update.setter
    def update(self, update: bool) -> None:
        """Set if data should be updated by central coordinator."""
        if self._update != update:
            self._update = update
            self._central_coordinator.configure_scheduled_refreshes()

    async def _async_cfg_entry_updated(self, *_) -> None:
        """Run when the config entry has been updated."""
        self.name = self.config_entry.title

    async def _async_first_refresh(self) -> Members:
        """Perform first refresh."""
        self.update_method = None
        await self._central_coordinator.async_add_coordinator(self)
        return self.data

    @callback
    def _unschedule_refresh(self) -> None:
        """Do not get periodic updates since there is no longer any listeners."""
        self.update = False

    @callback
    def _schedule_refresh(self) -> None:
        """Get periodic updates since there is at least one listener."""
        self.update = True


@dataclass
class ConfigData:
    """Data associated with config entry."""

    api: Life360
    coordinator: Life360DataUpdateCoordinator


ConfigMembers = dict[str, Members]


class Life360CentralDataUpdateCoordinator(DataUpdateCoordinator[None]):
    """Life360 central data update coordinator."""

    data: None
    _setup_complete = False
    _pref_disable_polling = True
    _remove_self_listener: CALLBACK_TYPE | None = None
    _refresh_locked_for_config: str | None = None
    _update_task: asyncio.Task[None] | None = None
    _scheduled_refresh: bool
    _cancellable_update: bool

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize data update coordinator."""
        super().__init__(hass, LOGGER, name=DOMAIN, update_interval=UPDATE_INTERVAL)
        # Central coordinator should not be associated with any particular config entry.
        self.config_entry = None

        self._entity_reg = entity_registry.async_get(hass)
        self._configs: dict[str, ConfigData] = {}
        self._refresh_lock = asyncio.Lock()
        self._logged: dict[CircleID, set[PlaceID]] = {}

        @callback
        def life360_loaded(event: Event) -> bool:
            """Return if life360 integration was loaded."""
            return event.data[ATTR_COMPONENT] == DOMAIN

        async def async_setup_done(*_) -> None:
            """Indicate setup has completed and initiate first full refresh."""
            self._setup_complete = True
            self._update_pref_disable_polling(
                all(
                    cfg_data.coordinator.config_entry.pref_disable_polling
                    for cfg_data in self._configs.values()
                )
            )
            if self._configs:
                await self.async_refresh()

        hass.bus.async_listen(EVENT_COMPONENT_LOADED, async_setup_done, life360_loaded)

    async def async_add_coordinator(
        self, coordinator: Life360DataUpdateCoordinator
    ) -> None:
        """Add a config entry & its coordinator."""
        # See if server can be accessed successfully. Let caller handle any exception.
        api = Life360(
            timeout=COMM_TIMEOUT,
            max_retries=COMM_MAX_RETRIES,
            authorization=coordinator.config_entry.data[CONF_AUTHORIZATION],
        )
        await self._async_retrieve_data(api, "get_circles")

        # Success. Add api & config and refresh if setup complete.

        # If this is the second phase of a reload, then lock has already been obtained.
        # If not, get the lock now.
        have_lock = self._refresh_locked_for_config == coordinator.config_entry.entry_id
        if not have_lock:
            await self._refresh_lock.acquire()

        self._configs[coordinator.config_entry.entry_id] = ConfigData(api, coordinator)
        coordinator.config_entry.async_on_unload(
            coordinator.config_entry.add_update_listener(self._async_cfg_entry_updated)
        )

        try:
            if self._setup_complete:
                await self._async_cfg_entry_updated(None, coordinator.config_entry)
                await self._async_refresh(log_failures=True, cancellable=False)
            else:
                coordinator.async_set_updated_data(Members())
        finally:
            if have_lock:
                self._refresh_locked_for_config = None
            self._refresh_lock.release()

    async def async_unloading_config(self, entry: ConfigEntry) -> None:
        """Config entry is being unloaded."""
        # If a cancellable refresh is in progress, cancel it, but either way, prevent
        # another from starting before removing config so there is no chance any Member
        # assigned to this config will get reassigned before unload is complete (which
        # could cause Entity Registry updates from unloading and refresh tasks to
        # interfer with each other.)
        if self._update_task:
            self._update_task.cancel()
        await self._refresh_lock.acquire()
        del self._configs[entry.entry_id]

        def done(*, release_lock=True, ok_to_refresh=True) -> None:
            if release_lock:
                self._refresh_lock.release()
            self.configure_scheduled_refreshes()
            if ok_to_refresh and self._configs:
                self.hass.async_create_task(self.async_refresh())

        if self._members_assigned(entry.entry_id):
            # Members are assigned to this config so wait for unload to complete.
            # Also, if this is the first phase of a reload, signal to setup of same
            # config that lock has already been obtained.
            if is_reload := entry.reload_lock.locked():
                self._refresh_locked_for_config = entry.entry_id

            async def async_wait_for_unload_done():
                await unload_done.wait()
                if is_reload:
                    # Wait for reload to complete.
                    await entry.reload_lock.acquire()
                    entry.reload_lock.release()
                    # If setup phase didn't run then release the lock here.
                    still_have_lock = self._refresh_locked_for_config == entry.entry_id
                    if still_have_lock:
                        self._refresh_locked_for_config = None
                else:
                    still_have_lock = True
                done(
                    release_lock=still_have_lock,
                    # Only refresh if setup phase didn't do so already.
                    ok_to_refresh=entry.state is not ConfigEntryState.LOADED,
                )

            unload_done = asyncio.Event()
            entry.async_on_unload(unload_done.set)
            # Wait for unload to complete in a separate task so this one can finish the
            # unload process.
            self.hass.async_create_task(async_wait_for_unload_done())

        else:
            # No Members assigned to config so go ahead and finish now.
            done()

    def configure_scheduled_refreshes(self) -> None:
        """Enable or disable periodic updating."""
        do_scheduled_updates = not self._pref_disable_polling and any(
            cfg_data.coordinator.update for cfg_data in self._configs.values()
        )
        if do_scheduled_updates and not self._remove_self_listener:
            # Add a listener to enable periodic updates.
            self._remove_self_listener = self.async_add_listener(lambda: None)
        elif not do_scheduled_updates and self._remove_self_listener:
            self._remove_self_listener()
            self._remove_self_listener = None

    def config_coordinator(self, cfg_id: str) -> Life360DataUpdateCoordinator:
        """Return coordinator for config."""
        return self._configs[cfg_id].coordinator

    async def async_refresh(self) -> None:
        """Refresh data and log errors."""
        async with self._refresh_lock:
            await super().async_refresh()

    async def _handle_refresh_interval(self, _now: datetime) -> None:
        """Handle a refresh interval occurrence."""
        async with self._refresh_lock:
            await super()._handle_refresh_interval(_now)

    def _members_assigned(self, cfg_id: str) -> bool:
        """Return if config has any Members assigned."""
        for reg_entry in self._entity_reg.entities.values():
            if (
                reg_entry.domain == Platform.DEVICE_TRACKER
                and reg_entry.platform == DOMAIN
                and reg_entry.config_entry_id == cfg_id
            ):
                return True
        return False

    async def _async_cfg_entry_updated(self, _, cfg_entry: ConfigEntry) -> None:
        """Run when a config entry has been updated."""
        if self._pref_disable_polling != cfg_entry.pref_disable_polling:
            self._update_pref_disable_polling(cfg_entry.pref_disable_polling)

    def _update_pref_disable_polling(self, pref_disable_polling) -> None:
        """Update pref_disable_polling for all configs."""
        self._pref_disable_polling = pref_disable_polling
        for cfg_data in self._configs.values():
            self.hass.config_entries.async_update_entry(
                cfg_data.coordinator.config_entry,
                pref_disable_polling=pref_disable_polling,
            )
        self.configure_scheduled_refreshes()

    async def _async_refresh(
        self,
        log_failures=True,
        raise_on_auth_failed=False,
        scheduled=False,
        cancellable=True,
    ) -> None:
        """Refresh data."""

        # In order to properly handle Member -> Config Entry assignment, all currently
        # registered accounts (i.e., Config Entries) must get a chance to be setup and,
        # hence, added as clients. Wait until that has happened before performing any
        # refresh.
        if not self._setup_complete:
            return

        self._scheduled_refresh = scheduled
        self._cancellable_update = cancellable
        await super()._async_refresh(log_failures, raise_on_auth_failed, scheduled)
        if not self.last_update_success:
            exc = cast(Exception, self.last_exception)
            for cfg_data in self._configs.values():
                cfg_data.coordinator.async_set_update_error(exc)

    async def _async_update_data(self) -> None:
        """Get & process data from Life360."""
        circles: dict[CircleID, Circle] = {}
        members: dict[MemberID, list[tuple[CircleID, Member]]] = {}
        cfg_circle_ids: dict[str, set[CircleID] | None] = {}

        if self._cancellable_update:
            self._update_task = asyncio.current_task()

        try:
            for cfg_id, cfg_data in self._configs.items():
                try:
                    circle_ids = await self._async_retrieve_config_data(
                        cfg_id, circles, members
                    )
                except ConfigEntryAuthFailed as exc:
                    cfg_data.coordinator.async_set_update_error(exc)
                    cfg_data.coordinator.config_entry.async_start_reauth(self.hass)
                    cfg_circle_ids[cfg_id] = None
                else:
                    cfg_circle_ids[cfg_id] = circle_ids

        except asyncio.CancelledError:
            return None
        finally:
            self._update_task = None

        self._log_new_circles_and_places(circles)
        result = self._assign_members(
            circles, cfg_circle_ids, self._group_sort_members(members)
        )
        self._dump_result(result)
        for cfg_id, data in result.items():
            coordinator = self._configs[cfg_id].coordinator
            if coordinator.update or not self._scheduled_refresh:
                coordinator.async_set_updated_data(data)

        return None

    async def _async_retrieve_config_data(
        self,
        cfg_id: str,
        circles: dict[CircleID, Circle],
        members: dict[MemberID, list[tuple[CircleID, Member]]],
    ) -> set[CircleID]:
        """Retrieve data using a Life360 account."""
        LOGGER.info(
            "Retrieving data for %s",
            self._configs[cfg_id].coordinator.config_entry.title,
        )
        api = self._configs[cfg_id].api

        circle_ids: set[CircleID] = set()
        new_circles: dict[CircleID, Circle] = {}
        found_members: dict[MemberID, list[tuple[CircleID, Member]]] = {}

        for circle_data in await self._async_retrieve_data(api, "get_circles"):
            circle_id = CircleID(circle_data["id"])

            # Keep track of which circles config has access to.
            circle_ids.add(circle_id)
            # First time we see a circle retrieve all its data.
            if circle_id not in circles:
                circle_places, circle_members = await asyncio.gather(
                    self._async_retrieve_data(api, "get_circle_places", circle_id),
                    self._async_retrieve_data(api, "get_circle_members", circle_id),
                )

                # Process Places in this Circle.
                # Record which config was used to retrieve the Circle data.
                new_circles[circle_id] = Circle(
                    circle_data["name"],
                    {
                        place_data["id"]: Place(
                            place_data["name"],
                            float(place_data["latitude"]),
                            float(place_data["longitude"]),
                            float(place_data["radius"]),
                        )
                        for place_data in circle_places
                    },
                    cfg_id,
                )

                # Process Members in this Circle.
                # Keep track of which Circle the data came from.
                for member_data in circle_members:
                    member_id, member = self._process_member_data(member_data)
                    found_members.setdefault(member_id, []).append((circle_id, member))

        circles.update(new_circles)
        for member_id, cid_mem_list in found_members.items():
            members.setdefault(member_id, []).extend(cid_mem_list)

        return circle_ids

    async def _async_retrieve_data(
        self, api: Life360, func: str, *args: Any
    ) -> list[dict[str, Any]]:
        """Get data from Life360."""
        try:
            return await self.hass.async_add_executor_job(getattr(api, func), *args)
        except LoginError as exc:
            LOGGER.debug("Login error: %s", exc)
            raise ConfigEntryAuthFailed(exc) from exc
        except Life360Error as exc:
            LOGGER.debug("%s: %s", exc.__class__.__name__, exc)
            raise UpdateFailed(exc) from exc

    def _process_member_data(
        self, member_data: dict[str, Any]
    ) -> tuple[MemberID, Member]:
        """Process raw member data from server."""
        member_id: MemberID = member_data["id"]
        first: str | None = member_data["firstName"]
        last: str | None = member_data["lastName"]
        if first and last:
            name = " ".join([first, last])
        else:
            name = first or last or "No Name"
        entity_picture: str | None = member_data["avatar"]

        if not int(member_data["features"]["shareLocation"]):
            # Member isn't sharing location with this Circle.
            return (
                member_id,
                Member(name, entity_picture, status=MemberStatus.NOT_SHARING),
            )

        loc: dict[str, Any] | None
        if not (loc := member_data["location"]):
            err_msg: str | None
            extended_reason: str | None
            if err_msg := member_data["issues"]["title"]:
                if extended_reason := member_data["issues"]["dialog"]:
                    err_msg += f": {extended_reason}"
                status = MemberStatus.MISSING_W_REASON
            else:
                err_msg = (
                    "The user may have lost connection to Life360. "
                    "See https://www.life360.com/support/"
                )
                status = MemberStatus.MISSING_NO_REASON
            return (
                member_id,
                Member(name, entity_picture, status=status, err_msg=err_msg),
            )

        place: str | None = loc["name"] or None

        address1: str | None = loc["address1"] or None
        address2: str | None = loc["address2"] or None
        if address1 and address2:
            address: str | None = ", ".join([address1, address2])
        else:
            address = address1 or address2

        speed = max(0, float(loc["speed"]) * SPEED_FACTOR_MPH)
        if self.hass.config.units.is_metric:
            speed = convert(speed, LENGTH_MILES, LENGTH_KILOMETERS)

        return (
            member_id,
            Member(
                name,
                entity_picture,
                MemberLocation(
                    address,
                    dt_util.utc_from_timestamp(int(loc["since"])),
                    bool(int(loc["isDriving"])),
                    # Life360 reports accuracy in feet, but Device Tracker expects
                    # gps_accuracy in meters.
                    round(convert(float(loc["accuracy"]), LENGTH_FEET, LENGTH_METERS)),
                    dt_util.utc_from_timestamp(int(loc["timestamp"])),
                    float(loc["latitude"]),
                    float(loc["longitude"]),
                    place,
                    round(speed, SPEED_DIGITS),
                ),
                bool(int(loc["charge"])),
                int(float(loc["battery"])),
                bool(int(loc["wifiState"])),
            ),
        )

    def _log_new_circles_and_places(self, circles: dict[CircleID, Circle]) -> None:
        """Log any new Circles and Places."""
        for circle_id, circle in circles.items():
            if circle_id not in self._logged:
                LOGGER.debug("Circle: %s", circle.name)
                self._logged[circle_id] = set()
            if new_places := set(circle.places) - self._logged[circle_id]:
                self._logged[circle_id] |= new_places
                msg = f"Places from {circle.name}:"
                for place_id in new_places:
                    place = circle.places[place_id]
                    msg += f"\n- name: {place.name}"
                    msg += f"\n  latitude: {place.latitude}"
                    msg += f"\n  longitude: {place.longitude}"
                    msg += f"\n  radius: {place.radius}"
                LOGGER.debug(msg)

    def _group_sort_members(
        self,
        members: dict[MemberID, list[tuple[CircleID, Member]]],
    ) -> dict[MemberID, dict[MemberStatus, tuple[Member, tuple[CircleID]]]]:
        """Group and sort Member results."""
        # For each MemberID, group results by MemberStatus, and for each group find the
        # best Member data, and all the CircleIDs that saw the Member with that same
        # status, but also with the CircleIDs sorted with the Circles that saw the best
        # data (within that group) first.
        mem_cids_per_status: dict[
            MemberID, dict[MemberStatus, tuple[Member, tuple[CircleID]]]
        ] = {}
        for member_id, cid_mem_list in members.items():
            mem_cids_per_status[member_id] = {}
            for status, group in groupby(
                sorted(
                    cid_mem_list, key=lambda cid_member: cid_member[1], reverse=True
                ),
                lambda cid_member: cid_member[1].status,
            ):
                cids, mems = cast(
                    tuple[tuple[CircleID], tuple[Member]],
                    tuple(zip(*group)),
                )
                mem_cids_per_status[member_id][status] = max(mems), cids
        return mem_cids_per_status

    def _assign_members(
        self,
        circles: dict[CircleID, Circle],
        cfg_circle_ids: dict[str, set[CircleID] | None],
        mem_cids_per_status: dict[
            MemberID, dict[MemberStatus, tuple[Member, tuple[CircleID]]]
        ],
    ) -> ConfigMembers:
        """Assign Members to appropriate config entries."""

        # If a Member can be seen via multiple config entries, choose the one that sees
        # that Member the 'best' (e.g., where all location data is available.) But, if
        # that Member is already assigned to a config entry, try to keep that
        # association.

        # The process may involve moving a Member from one config entry to another if a
        # different config entry can see the Member 'better'; e.g., the Member is no
        # longer sharing their location with any Circle the originally assigned config
        # entry can see, but is sharing with a Circle that can be seen by another config
        # entry.

        # Also handle a Member that is no longer seen by any visible Circle. E.g.,
        # Member may have deleted their Life360 account, or has left all the Circles
        # that can be seen via the configured Life360 accounts, or these accounts can no
        # longer see any Circles that the Member is in (e.g., account user has left
        # those Circles), etc.

        auth_failures = {
            cfg_id for cfg_id in self._configs if cfg_circle_ids[cfg_id] is None
        }
        assignable_configs = set(self._configs) - auth_failures
        result = ConfigMembers({cfg_id: Members() for cfg_id in assignable_configs})

        # Determine how to handle Members, either previously seen or newly seen.
        # Note that any Member currently assigned to a config entry for which there was
        # a login error will be ignored until either the error is cleared or the config
        # entry is disabled/deleted. These Members will show as unavailable.
        registered_members: set[MemberID] = set()
        current_member_assignments: dict[MemberID, str] = {}
        keep_assigned_members: set[MemberID] = set()
        for reg_entry in self._entity_reg.entities.values():
            if (
                reg_entry.domain != Platform.DEVICE_TRACKER
                or reg_entry.platform != DOMAIN
            ):
                continue
            registered_members.add(member_id := MemberID(reg_entry.unique_id))
            if cfg_id := reg_entry.config_entry_id:
                current_member_assignments[member_id] = cfg_id
                if cfg_id in auth_failures:
                    keep_assigned_members.add(member_id)
        assigned_members = set(current_member_assignments)
        reassignable_members = assigned_members - keep_assigned_members
        seen_members = set(mem_cids_per_status)

        check_assignments = reassignable_members & seen_members
        remove_assignments = reassignable_members - seen_members
        make_assignments = seen_members - assigned_members

        LOGGER.info("auth_failures: %s", auth_failures)
        LOGGER.info("assignable_configs: %s", assignable_configs)
        self._dump_result(result, msg="_assign_members start", short=True)
        LOGGER.info("registered_members: %s", registered_members)
        LOGGER.info("current_member_assignments: %s", current_member_assignments)
        LOGGER.info("keep_assigned_members: %s", keep_assigned_members)
        LOGGER.info("seen_members: %s", seen_members)
        LOGGER.info("remove_assignments: %s", remove_assignments)
        LOGGER.info("make_assignments: %s", make_assignments)
        LOGGER.info("check_assignments: %s", check_assignments)

        def find_a_config(cfg_ids: set[str], circle_ids: tuple[CircleID]) -> str | None:
            """Find a config that saw one of the Circles."""
            # Circles with best data come first.
            for circle_id in circle_ids:
                # See if config that was used to actually fetch the Circle's data can
                # be used.
                if (cfg_id := circles[circle_id].cfg_id) in cfg_ids:
                    return cfg_id
                # Try to find another config that saw this Circle
                for cfg_id in cfg_ids:
                    if circle_id in cast(set[CircleID], cfg_circle_ids[cfg_id]):
                        return cfg_id
            return None

        for member_id in check_assignments:
            cur_cfg_id = current_member_assignments[member_id]
            cur_cfg_entry = self.hass.config_entries.async_get_entry(cur_cfg_id)

            new_cfg_id: str | None = None
            for member, circle_ids in mem_cids_per_status[member_id].values():
                if cur_cfg_id in assignable_configs and cast(
                    set[CircleID], cfg_circle_ids[cur_cfg_id]
                ) & set(circle_ids):
                    new_cfg_id = cur_cfg_id
                else:
                    new_cfg_id = find_a_config(
                        assignable_configs - {cur_cfg_id}, circle_ids
                    )
                if new_cfg_id:
                    break

            if not new_cfg_id:
                remove_assignments.add(member_id)
                continue

            # pylint doesn't understand that member variable will always be valid at
            # this point.
            # pylint: disable=undefined-loop-variable

            if new_cfg_id == cur_cfg_id:
                reg_entry = self._update_entity_registry(member_id, name=member.name)
                result[cur_cfg_id][member_id] = member
                LOGGER.info(
                    "%s keeping assigned to %s",
                    _member_str(reg_entry, member_id),
                    cast(ConfigEntry, cur_cfg_entry).title,
                )
                self._dump_result(result, short=True)
                continue

            reg_entry = self._update_entity_registry(
                member_id, cfg_id=new_cfg_id, name=member.name
            )
            if not reg_entry.disabled:
                result[new_cfg_id][member_id] = member

            if cur_cfg_entry:
                cur_account = f"account {cur_cfg_entry.title}"
            else:
                cur_account = f"deleted account <{cur_cfg_id}>"
            new_cfg_entry = self._configs[new_cfg_id].coordinator.config_entry
            LOGGER.debug(
                "%s reassigned from %s to %s%s",
                _member_str(reg_entry, member_id),
                cur_account,
                new_cfg_entry.title,
                ": disabled" if reg_entry.disabled else "",
            )
            self._dump_result(result, short=True)

        for member_id in remove_assignments:
            # Disconnect entity from config entry. This will cause the corresponding
            # entity to be removed, but it will remain in the entity registry in case
            # Member becomes visible again. Or the user can decide to delete the entity
            # registry entry.
            reg_entry = self._update_entity_registry(member_id, cfg_id=None)
            LOGGER.warning(
                "%s is no longer in any visible Circle", _member_str(reg_entry)
            )
            LOGGER.debug("%s is no longer visible", _member_str(reg_entry, member_id))

        for member_id in make_assignments:
            cfg_id = None
            for member, circle_ids in mem_cids_per_status[member_id].values():
                if cfg_id := find_a_config(assignable_configs, circle_ids):
                    break
            if not cfg_id:
                continue

            cfg_entry = self._configs[cfg_id].coordinator.config_entry
            if member_id in registered_members:
                reg_entry = self._update_entity_registry(
                    member_id, cfg_id=cfg_id, name=member.name
                )
            else:
                reg_entry = self._entity_reg.async_get_or_create(
                    Platform.DEVICE_TRACKER,
                    DOMAIN,
                    member_id,
                    suggested_object_id=member.name,
                    config_entry=cfg_entry,
                    original_name=member.name,
                )
            if not reg_entry.disabled:
                result[cfg_id][member_id] = member

            LOGGER.debug(
                "%s assigned to account %s%s",
                _member_str(reg_entry, member_id),
                cfg_entry.title,
                ": disabled" if reg_entry.disabled else "",
            )
            self._dump_result(result, short=True)

        return result

    def _update_entity_registry(
        self,
        member_id: MemberID,
        *,
        cfg_id: str | None | UndefinedType = UNDEFINED,
        name: str | UndefinedType = UNDEFINED,
    ) -> RegistryEntry:
        """Update Entity Registry entry for Member.

        Returns new Entity Registry entry.
        """
        entity_id = cast(
            str,
            self._entity_reg.async_get_entity_id(
                Platform.DEVICE_TRACKER, DOMAIN, member_id
            ),
        )
        reg_entry = self._entity_reg.entities[entity_id]
        if cfg_id is UNDEFINED or reg_entry.disabled_by is RegistryEntryDisabler.USER:
            disable_by: RegistryEntryDisabler | UndefinedType | None = UNDEFINED
        elif (
            cfg_id
            and self._configs[cfg_id].coordinator.config_entry.pref_disable_new_entities
        ):
            disable_by = RegistryEntryDisabler.INTEGRATION
        else:
            disable_by = None
        return self._entity_reg.async_update_entity(
            entity_id,
            config_entry_id=cfg_id,
            disabled_by=disable_by,
            original_name=name,
        )

    def _dump_result(self, result, msg="", short=False):
        if msg:
            msg += ": "
        msg += "result:"
        if len(result):
            for cfg_id, mems in result.items():
                cfg_entry = cast(
                    ConfigEntry, self.hass.config_entries.async_get_entry(cfg_id)
                )
                msg += f"\n  {cfg_id} {cfg_entry.title}:"
                if len(mems):
                    if short:
                        msg += f" {{{', '.join(f'{mem_id}: {mem.name if mem else None}' for mem_id, mem in mems.items())}}}"
                    else:
                        for mem_id, mem in mems.items():
                            msg += f"\n    {mem_id}:"
                            msg += f"\n      {mem.name}"
                            msg += f"\n      {mem.loc}"
                            msg += f"\n      {mem.status.name}"
                            msg += f"\n      {mem.err_msg}"
                else:
                    msg += f" {mems}"
        else:
            msg += f" {result}"
        LOGGER.info(msg)


def _member_str(reg_entry: RegistryEntry, member_id: MemberID | None = None) -> str:
    """Return a string identifying Member."""
    name = cast(str, reg_entry.name or reg_entry.original_name)
    entity_id = reg_entry.entity_id
    if member_id:
        return f"{name} ({member_id} -> {entity_id})"
    return f"{name} ({entity_id})"
