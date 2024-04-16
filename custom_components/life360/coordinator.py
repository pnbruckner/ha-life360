"""DataUpdateCoordinator for the Life360 integration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum, auto
from functools import partial
import logging
from math import ceil
from typing import Any, Self, TypeVar, TypeVarTuple, cast

from life360 import Life360Error, LoginError, NotModified, RateLimited

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from . import helpers
from .const import (
    CIRCLE_UPDATE_INTERVAL,
    COMM_MAX_RETRIES,
    COMM_TIMEOUT,
    DOMAIN,
    STORAGE_KEY,
    STORAGE_VERSION,
    UPDATE_INTERVAL,
)
from .helpers import ConfigOptions, MemberData

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")
_R = TypeVar("_R")
_Ts = TypeVarTuple("_Ts")
_StoredData = dict[str, dict[str, dict[str, Any]]]


@dataclass
class CircleData:
    """Circle data."""

    name: str
    unames: list[str] = field(default_factory=list)
    mids: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Initialize from dict."""
        return cls(
            cast(str, data["name"]),
            cast(list[str], data["unames"]),
            cast(list[str], data["mids"]),
        )


@dataclass
class CircleMemberData:
    """Circle & Member data."""

    circles: dict[str, CircleData] = field(default_factory=dict)
    # TODO: Include Member name somewhere, too???
    mem_circles: dict[str, list[str]] = field(default_factory=dict)


class RateLimitedAction(Enum):
    """Action to take when rate limited."""

    ERROR = auto()
    WARNING = auto()
    RETRY = auto()


class RequestError(Enum):
    """Request error type."""

    NOT_MODIFIED = auto()
    RATE_LIMITED = auto()
    NO_DATA = auto()


class Life360DataUpdateCoordinator(DataUpdateCoordinator[dict[str, MemberData]]):
    """Life360 data update coordinator."""

    config_entry: ConfigEntry
    __cm_data: CircleMemberData | None = None
    _updating_cm_data: bool = False

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize data update coordinator."""
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=UPDATE_INTERVAL)
        options = ConfigOptions.from_dict(self.config_entry.options)
        self._store = Store[_StoredData](self.hass, STORAGE_VERSION, STORAGE_KEY)
        self._apis = {
            uname: helpers.Life360(
                async_create_clientsession(hass, timeout=COMM_TIMEOUT),
                COMM_MAX_RETRIES,
                acct.authorization,
                verbosity=options.verbosity,
            )
            for uname, acct in options.accounts.items()
            if acct.enabled
        }
        self._login_error: list[str] = []
        # {mid: {cid: MemberData}}
        self._member_circle_data: dict[str, dict[str, MemberData]] = {}

    async def _async_update_data(self) -> dict[str, MemberData]:
        """Fetch the latest data from the source."""
        # TODO: How to handle errors, especially per uname/api???
        result: dict[str, MemberData] = {}

        cm_data = await self._cm_data()
        raw_member_list_list = await asyncio.gather(
            *(self._get_raw_member_list(mid, cm_data) for mid in cm_data.mem_circles)
        )
        for mid, raw_member_list in zip(cm_data.mem_circles, raw_member_list_list):
            cids = cm_data.mem_circles[mid]
            member_circle_data: dict[str, MemberData] = {}
            for cid, raw_member in zip(cids, raw_member_list):
                if raw_member is RequestError.NOT_MODIFIED:
                    member_circle_data[cid] = self._member_circle_data[mid][cid]
                elif not isinstance(raw_member, RequestError):
                    member_circle_data[cid] = MemberData.from_server(raw_member)
            self._member_circle_data[mid] = member_circle_data
            result[mid] = sorted(member_circle_data.values())[-1]

        return result

    async def _cm_data(self) -> CircleMemberData:
        """Return current Circle & Member data."""
        if not self.__cm_data:
            # Try to get Circles & Members from storage.
            self.__cm_data = await self._load_cm_data()
            run_now = False
            if not self.__cm_data:
                # Get Circles & Members from server, returning immediately with whatever
                # data is available.
                self.__cm_data, run_now = await self._get_cm_data(at_startup=True)

            # eager_start was added in 2024.3.
            start_updating = partial(
                self.config_entry.async_create_background_task,
                self.hass,
                self._start_cm_data_updating(run_now=run_now),
                "Start periodic Circle & Member updating",
            )
            try:
                start_updating(eager_start=True)
            except TypeError:
                start_updating()

        return self.__cm_data

    async def _load_cm_data(self) -> CircleMemberData | None:
        """Load Circles & Members from storage."""
        if not (store_data := await self._load_store()):
            _LOGGER.warning(
                "Could not load Circles & Members from storage"
                "; will use whatever data is immediately available from server"
            )
            return None

        circles: dict[str, CircleData] = {}
        mem_circles: dict[str, list[str]] = {}
        for cid, circle_data_dict in store_data["circles"].items():
            circle_data = CircleData.from_dict(circle_data_dict)
            circles[cid] = circle_data
            for mid in circle_data.mids:
                mem_circles.setdefault(mid, []).append(cid)
        return CircleMemberData(circles, mem_circles)

    async def _load_store(self) -> _StoredData | None:
        """Load data from storage."""
        try:
            return await self._store.async_load()
        except Exception:
            # TODO: How to handle this properly?
            _LOGGER.exception("While loading Circles & Members from storage")
            return None

    async def _get_cm_data(self, at_startup: bool) -> tuple[CircleMemberData, bool]:
        """Get Life360 Circles & Members seen from all enabled accounts.

        Returns CircleMemberData and a bool that is True if any requests were rate
        limited.
        """
        if self.__cm_data:
            old_circles = self.__cm_data.circles
        else:
            old_circles = {}
        unames = list(self._apis)

        raw_circles_list = await self._get_circles(unames, at_startup)

        circles: dict[str, CircleData] = {}
        rate_limited = False
        for uname, raw_circles in zip(unames, raw_circles_list):
            if raw_circles is RequestError.NOT_MODIFIED:
                for cid, circle_data in old_circles.items():
                    if uname not in circle_data.unames:
                        continue
                    if cid not in circles:
                        circles[cid] = CircleData(circle_data.name)
                    circles[cid].unames.append(uname)
            elif isinstance(raw_circles, RequestError):
                if raw_circles is RequestError.RATE_LIMITED:
                    rate_limited = True
            else:
                for raw_circle in raw_circles:
                    if (cid := raw_circle["id"]) not in circles:
                        circles[cid] = CircleData(raw_circle["name"])
                    circles[cid].unames.append(uname)

        cm_data = CircleMemberData(circles)

        for cid, circle_data in circles.items():
            for uname in circle_data.unames:
                raw_members = await self._request(
                    uname,
                    self._apis[uname].get_circle_members,
                    cid,
                    msg=f"while getting Members in {circle_data.name} Circle",
                )
                if not isinstance(raw_members, RequestError):
                    for raw_member in raw_members:
                        # TODO: Add Member name, too???
                        mid = cast(str, raw_member["id"])
                        circle_data.mids.append(mid)
                        cm_data.mem_circles.setdefault(mid, []).append(cid)
                    break

        if at_startup:
            store_data = cast(_StoredData, {})
        else:
            store_data = await self._load_store() or {}
        store_data["circles"] = {
            cid: asdict(circle_data) for cid, circle_data in cm_data.circles.items()
        }
        await self._store.async_save(store_data)

        return cm_data, rate_limited

    async def _get_circles(
        self, unames: Iterable[str], at_startup: bool
    ) -> list[list[dict[str, str]] | RequestError]:
        """Get Circles for each username."""
        rla = RateLimitedAction.WARNING if at_startup else RateLimitedAction.RETRY
        return await asyncio.gather(  # type: ignore[no-any-return]
            *(
                self.config_entry.async_create_background_task(
                    self.hass,
                    self._request(
                        uname,
                        partial(
                            self._apis[uname].get_circles,
                            raise_not_modified=not at_startup,
                        ),
                        msg="while getting Circles",
                        rate_limited_action=rla,
                    ),
                    f"Get Circles for {uname}",
                )
                for uname in unames
            )
        )

    async def _start_cm_data_updating(self, run_now: bool) -> None:
        """Start periodic updating of Circles & Members data."""
        if run_now:
            self.__cm_data, _ = await self._get_cm_data(at_startup=False)
        self.config_entry.async_on_unload(
            async_track_time_interval(
                self.hass, self._update_cm_data, CIRCLE_UPDATE_INTERVAL
            )
        )

    async def _update_cm_data(self, now: datetime) -> None:
        """Update Circles & Members data."""
        # Guard against being called again while previous call is still in progress.
        if self._updating_cm_data:
            return
        self._updating_cm_data = True
        self.__cm_data, _ = await self._get_cm_data(at_startup=False)
        self._updating_cm_data = False

    async def _get_raw_member_list(
        self, mid: str, cm_data: CircleMemberData
    ) -> list[dict[str, Any] | RequestError]:
        """Get raw Member data from each Circle Member is in."""
        tasks: list[asyncio.Task[dict[str, Any] | RequestError]] = []
        for cid in cm_data.mem_circles[mid]:
            circle_data = cm_data.circles[cid]
            tasks.append(
                self.config_entry.async_create_background_task(
                    self.hass,
                    self._get_raw_member(mid, cid, circle_data),
                    f"Get Member from {circle_data.name}",
                )
            )
        return await asyncio.gather(*tasks)

    async def _get_raw_member(
        self, mid: str, cid: str, circle_data: CircleData
    ) -> dict[str, Any] | RequestError:
        """Get raw Member data from given Circle."""
        for uname in circle_data.unames:
            raw_member = await self._request(
                uname,
                partial(
                    self._apis[uname].get_circle_member,
                    cid,
                    mid,
                    raise_not_modified=True,
                ),
                msg=f"while getting Member from {circle_data.name} Circle",
            )
            if raw_member is RequestError.NOT_MODIFIED:
                return RequestError.NOT_MODIFIED
            if not isinstance(raw_member, RequestError):
                return raw_member  # type: ignore[no-any-return]
        return RequestError.NO_DATA

    # TODO: Add some overall timeout to make sure rate limiting & retrying can't make it take forever???
    async def _request(
        self,
        uname: str,
        target: Callable[[*_Ts], Coroutine[Any, Any, _R]],
        *args: *_Ts,
        msg: str | None = None,
        rate_limited_action: RateLimitedAction = RateLimitedAction.ERROR,
    ) -> _R | RequestError:
        """Make a request to the Life360 server."""
        if uname in self._login_error:
            return RequestError.NO_DATA

        while True:
            try:
                return await target(*args)
            except NotModified:
                return RequestError.NOT_MODIFIED
            except LoginError as exc:
                _LOGGER.error("%s: login error %s: %s", uname, msg, exc)
                await self._handle_login_error(uname)
                return RequestError.NO_DATA
            except Life360Error as exc:
                level = logging.ERROR
                result = RequestError.NO_DATA
                if isinstance(exc, RateLimited):
                    if rate_limited_action is RateLimitedAction.RETRY:
                        delay = ceil(exc.retry_after or 0) + 10
                        _LOGGER.debug(
                            "%s: rate limited %s: will retry in %i s",
                            uname,
                            msg,
                            delay,
                        )
                        await asyncio.sleep(delay)
                        continue
                    if rate_limited_action is RateLimitedAction.WARNING:
                        level = logging.WARNING
                    result = RequestError.RATE_LIMITED
                # TODO: Keep track of errors per uname so we don't flood log???
                #       Maybe like DataUpdateCoordinator does it?
                _LOGGER.log(level, "%s: while getting Circles: %s", uname, exc)
                return result

    async def _handle_login_error(self, uname: str) -> None:
        """Handle account login error."""
        self._login_error.append(uname)
        # TODO: Log repair issue.
        # TODO: How to "reactivate" account (i.e., remove from self._login_error)???


#         circles: dict[CircleID, Circle] = {}
#         members: dict[MemberID, list[tuple[CircleID, Member]]] = {}
#         cfg_circle_ids: dict[str, set[CircleID] | None] = {}

#         self._update_task = asyncio.current_task()

#         try:
#             for cfg_id, cfg_data in self._configs.items():
#                 try:
#                     circle_ids = await self._async_retrieve_config_data(
#                         cfg_id, circles, members
#                     )
#                 except ConfigEntryAuthFailed as exc:
#                     cfg_data.coordinator.async_set_update_error(exc)
#                     cfg_data.coordinator.config_entry.async_start_reauth(self.hass)
#                     cfg_circle_ids[cfg_id] = None
#                 else:
#                     cfg_circle_ids[cfg_id] = circle_ids

#         except asyncio.CancelledError:
#             return None
#         finally:
#             self._update_task = None

#         self._log_new_circles_and_places(circles)
#         result = self._assign_members(
#             circles, cfg_circle_ids, self._group_sort_members(members)
#         )
#         self._dump_result(result)  # type: ignore[no-untyped-call]
#         for cfg_id, data in result.items():
#             coordinator = self._configs[cfg_id].coordinator
#             if coordinator.update or not self._scheduled_refresh:
#                 coordinator.async_set_updated_data(data)

#         return None

#     async def _async_retrieve_config_data(
#         self,
#         cfg_id: str,
#         circles: dict[CircleID, Circle],
#         members: dict[MemberID, list[tuple[CircleID, Member]]],
#     ) -> set[CircleID]:
#         """Retrieve data using a Life360 account."""
#         LOGGER.info(
#             "Retrieving data for %s",
#             self._configs[cfg_id].coordinator.config_entry.title,
#         )
#         api = self._configs[cfg_id].api

#         circle_ids: set[CircleID] = set()
#         new_circles: dict[CircleID, Circle] = {}
#         found_members: dict[MemberID, list[tuple[CircleID, Member]]] = {}

#         for circle_data in await self._async_retrieve_data(api, "get_circles"):
#             circle_id = CircleID(circle_data["id"])

#             # Keep track of which circles config has access to.
#             circle_ids.add(circle_id)
#             # First time we see a circle retrieve all its data.
#             if circle_id not in circles:
#                 circle_places, circle_members = await asyncio.gather(
#                     self._async_retrieve_data(api, "get_circle_places", circle_id),
#                     self._async_retrieve_data(api, "get_circle_members", circle_id),
#                 )

#                 # Process Places in this Circle.
#                 # Record which config was used to retrieve the Circle data.
#                 new_circles[circle_id] = Circle(
#                     circle_data["name"],
#                     {
#                         place_data["id"]: Place(
#                             place_data["name"],
#                             float(place_data["latitude"]),
#                             float(place_data["longitude"]),
#                             float(place_data["radius"]),
#                         )
#                         for place_data in circle_places
#                     },
#                     cfg_id,
#                 )

#                 # Process Members in this Circle.
#                 # Keep track of which Circle the data came from.
#                 for member_data in circle_members:
#                     member_id, member = self._process_member_data(member_data)
#                     found_members.setdefault(member_id, []).append((circle_id, member))

#         circles.update(new_circles)
#         for member_id, cid_mem_list in found_members.items():
#             members.setdefault(member_id, []).extend(cid_mem_list)

#         return circle_ids

#     async def _async_retrieve_data(
#         self, api: Life360, func: str, *args: Any
#     ) -> list[dict[str, Any]]:
#         """Get data from Life360."""
#         try:
#             return cast(list[dict[str, Any]], await getattr(api, func)(*args))
#         except LoginError as exc:
#             LOGGER.debug("Login error: %s", exc)
#             raise ConfigEntryAuthFailed(exc) from exc
#         except Life360Error as exc:
#             LOGGER.debug("%s: %s", exc.__class__.__name__, exc)
#             raise UpdateFailed(exc) from exc

#     def _process_member_data(
#         self, member_data: dict[str, Any]
#     ) -> tuple[MemberID, Member]:
#         """Process raw member data from server."""
#         member_id: MemberID = member_data["id"]
#         first: str | None = member_data["firstName"]
#         last: str | None = member_data["lastName"]
#         if first and last:
#             name = " ".join([first, last])
#         else:
#             name = first or last or "No Name"
#         entity_picture: str | None = member_data["avatar"]

#         if not int(member_data["features"]["shareLocation"]):
#             # Member isn't sharing location with this Circle.
#             return (
#                 member_id,
#                 Member(name, entity_picture, status=MemberStatus.NOT_SHARING),
#             )

#         loc: dict[str, Any] | None
#         if not (loc := member_data["location"]):
#             err_msg: str | None
#             extended_reason: str | None
#             if err_msg := member_data["issues"]["title"]:
#                 if extended_reason := member_data["issues"]["dialog"]:
#                     err_msg += f": {extended_reason}"
#                 status = MemberStatus.MISSING_W_REASON
#             else:
#                 err_msg = (
#                     "The user may have lost connection to Life360. "
#                     "See https://www.life360.com/support/"
#                 )
#                 status = MemberStatus.MISSING_NO_REASON
#             return (
#                 member_id,
#                 Member(name, entity_picture, status=status, err_msg=err_msg),
#             )

#         place: str | None = loc["name"] or None

#         address1: str | None = loc["address1"] or None
#         address2: str | None = loc["address2"] or None
#         if address1 and address2:
#             address: str | None = ", ".join([address1, address2])
#         else:
#             address = address1 or address2

#         speed = max(0, float(loc["speed"]) * SPEED_FACTOR_MPH)
#         if self.hass.config.units is METRIC_SYSTEM:
#             speed = convert(speed, LENGTH_MILES, LENGTH_KILOMETERS)

#         return (
#             member_id,
#             Member(
#                 name,
#                 entity_picture,
#                 MemberLocation(
#                     address,
#                     dt_util.utc_from_timestamp(int(loc["since"])),
#                     bool(int(loc["isDriving"])),
#                     # Life360 reports accuracy in feet, but Device Tracker expects
#                     # gps_accuracy in meters.
#                     round(convert(float(loc["accuracy"]), LENGTH_FEET, LENGTH_METERS)),
#                     dt_util.utc_from_timestamp(int(loc["timestamp"])),
#                     float(loc["latitude"]),
#                     float(loc["longitude"]),
#                     place,
#                     round(speed, SPEED_DIGITS),
#                 ),
#                 bool(int(loc["charge"])),
#                 int(float(loc["battery"])),
#                 bool(int(loc["wifiState"])),
#             ),
#         )

#     def _log_new_circles_and_places(self, circles: dict[CircleID, Circle]) -> None:
#         """Log any new Circles and Places."""
#         for circle_id, circle in circles.items():
#             if circle_id not in self._logged:
#                 LOGGER.debug("Circle: %s", circle.name)
#                 self._logged[circle_id] = set()
#             if new_places := set(circle.places) - self._logged[circle_id]:
#                 self._logged[circle_id] |= new_places
#                 msg = f"Places from {circle.name}:"
#                 for place_id in new_places:
#                     place = circle.places[place_id]
#                     msg += f"\n- name: {place.name}"
#                     msg += f"\n  latitude: {place.latitude}"
#                     msg += f"\n  longitude: {place.longitude}"
#                     msg += f"\n  radius: {place.radius}"
#                 LOGGER.debug(msg)

#     def _group_sort_members(
#         self,
#         members: dict[MemberID, list[tuple[CircleID, Member]]],
#     ) -> dict[MemberID, dict[MemberStatus, tuple[Member, tuple[CircleID]]]]:
#         """Group and sort Member results."""
#         # For each MemberID, group results by MemberStatus, and for each group find the
#         # best Member data, and all the CircleIDs that saw the Member with that same
#         # status, but also with the CircleIDs sorted with the Circles that saw the best
#         # data (within that group) first.
#         mem_cids_per_status: dict[
#             MemberID, dict[MemberStatus, tuple[Member, tuple[CircleID]]]
#         ] = {}
#         for member_id, cid_mem_list in members.items():
#             mem_cids_per_status[member_id] = {}
#             for status, group in groupby(
#                 sorted(
#                     cid_mem_list, key=lambda cid_member: cid_member[1], reverse=True
#                 ),
#                 lambda cid_member: cid_member[1].status,
#             ):
#                 cids, mems = cast(
#                     tuple[tuple[CircleID], tuple[Member]],
#                     tuple(zip(*group)),
#                 )
#                 mem_cids_per_status[member_id][status] = max(mems), cids
#         return mem_cids_per_status

#     def _assign_members(
#         self,
#         circles: dict[CircleID, Circle],
#         cfg_circle_ids: dict[str, set[CircleID] | None],
#         mem_cids_per_status: dict[
#             MemberID, dict[MemberStatus, tuple[Member, tuple[CircleID]]]
#         ],
#     ) -> ConfigMembers:
#         """Assign Members to appropriate config entries."""

#         # If a Member can be seen via multiple config entries, choose the one that sees
#         # that Member the 'best' (e.g., where all location data is available.) But, if
#         # that Member is already assigned to a config entry, try to keep that
#         # association.

#         # The process may involve moving a Member from one config entry to another if a
#         # different config entry can see the Member 'better'; e.g., the Member is no
#         # longer sharing their location with any Circle the originally assigned config
#         # entry can see, but is sharing with a Circle that can be seen by another config
#         # entry.

#         # Also handle a Member that is no longer seen by any visible Circle. E.g.,
#         # Member may have deleted their Life360 account, or has left all the Circles
#         # that can be seen via the configured Life360 accounts, or these accounts can no
#         # longer see any Circles that the Member is in (e.g., account user has left
#         # those Circles), etc.

#         auth_failures = {
#             cfg_id for cfg_id in self._configs if cfg_circle_ids[cfg_id] is None
#         }
#         assignable_configs = set(self._configs) - auth_failures
#         result = ConfigMembers({cfg_id: Members() for cfg_id in assignable_configs})

#         # Determine how to handle Members, either previously seen or newly seen.
#         # Note that any Member currently assigned to a config entry for which there was
#         # a login error will be ignored until either the error is cleared or the config
#         # entry is disabled/deleted. These Members will show as unavailable.
#         registered_members: set[MemberID] = set()
#         current_member_assignments: dict[MemberID, str] = {}
#         keep_assigned_members: set[MemberID] = set()
#         for reg_entry in self._entity_reg.entities.values():
#             if (
#                 reg_entry.domain != Platform.DEVICE_TRACKER
#                 or reg_entry.platform != DOMAIN
#             ):
#                 continue
#             registered_members.add(member_id := MemberID(reg_entry.unique_id))
#             if cfg_id := reg_entry.config_entry_id:
#                 current_member_assignments[member_id] = cfg_id
#                 if cfg_id in auth_failures:
#                     keep_assigned_members.add(member_id)
#         assigned_members = set(current_member_assignments)
#         reassignable_members = assigned_members - keep_assigned_members
#         seen_members = set(mem_cids_per_status)

#         check_assignments = reassignable_members & seen_members
#         remove_assignments = reassignable_members - seen_members
#         make_assignments = seen_members - assigned_members

#         LOGGER.info("auth_failures: %s", auth_failures)
#         LOGGER.info("assignable_configs: %s", assignable_configs)
#         self._dump_result(result, msg="_assign_members start", short=True)  # type: ignore[no-untyped-call]
#         LOGGER.info("registered_members: %s", registered_members)
#         LOGGER.info("current_member_assignments: %s", current_member_assignments)
#         LOGGER.info("keep_assigned_members: %s", keep_assigned_members)
#         LOGGER.info("seen_members: %s", seen_members)
#         LOGGER.info("remove_assignments: %s", remove_assignments)
#         LOGGER.info("make_assignments: %s", make_assignments)
#         LOGGER.info("check_assignments: %s", check_assignments)

#         def find_a_config(cfg_ids: set[str], circle_ids: tuple[CircleID]) -> str | None:
#             """Find a config that saw one of the Circles."""
#             # Circles with best data come first.
#             for circle_id in circle_ids:
#                 # See if config that was used to actually fetch the Circle's data can
#                 # be used.
#                 if (cfg_id := circles[circle_id].cfg_id) in cfg_ids:
#                     return cfg_id
#                 # Try to find another config that saw this Circle
#                 for cfg_id in cfg_ids:
#                     if circle_id in cast(set[CircleID], cfg_circle_ids[cfg_id]):
#                         return cfg_id
#             return None

#         for member_id in check_assignments:
#             cur_cfg_id = current_member_assignments[member_id]
#             cur_cfg_entry = self.hass.config_entries.async_get_entry(cur_cfg_id)

#             new_cfg_id: str | None = None
#             for member, circle_ids in mem_cids_per_status[member_id].values():
#                 if cur_cfg_id in assignable_configs and cast(
#                     set[CircleID], cfg_circle_ids[cur_cfg_id]
#                 ) & set(circle_ids):
#                     new_cfg_id = cur_cfg_id
#                 else:
#                     new_cfg_id = find_a_config(
#                         assignable_configs - {cur_cfg_id}, circle_ids
#                     )
#                 if new_cfg_id:
#                     break

#             if not new_cfg_id:
#                 remove_assignments.add(member_id)
#                 continue

#             # pylint doesn't understand that member variable will always be valid at
#             # this point.
#             # pylint: disable=undefined-loop-variable

#             if new_cfg_id == cur_cfg_id:
#                 reg_entry = self._update_entity_registry(member_id, name=member.name)
#                 result[cur_cfg_id][member_id] = member
#                 LOGGER.info(
#                     "%s keeping assigned to %s",
#                     _member_str(reg_entry, member_id),
#                     cast(ConfigEntry, cur_cfg_entry).title,
#                 )
#                 # self._dump_result(result, short=True)
#                 continue

#             new_cfg_entry = self._configs[new_cfg_id].coordinator.config_entry

#             old_reg_entry = self._reg_entry(member_id)
#             reg_entry = self._update_entity_registry(
#                 member_id, cfg_id=new_cfg_id, name=member.name
#             )
#             if not reg_entry.disabled:
#                 result[new_cfg_id][member_id] = member
#             elif not old_reg_entry.disabled:
#                 LOGGER.warning(
#                     "%s reassigned to %s, but it has "
#                     '"Enable newly added entities" turned off',
#                     _member_str(reg_entry),
#                     new_cfg_entry.title,
#                 )

#             if cur_cfg_entry:
#                 cur_account = f"account {cur_cfg_entry.title}"
#             else:
#                 cur_account = f"deleted account <{cur_cfg_id}>"
#             LOGGER.debug(
#                 "%s reassigned from %s to %s%s",
#                 _member_str(reg_entry, member_id),
#                 cur_account,
#                 new_cfg_entry.title,
#                 ": disabled" if reg_entry.disabled else "",
#             )
#             # self._dump_result(result, short=True)

#         for member_id in remove_assignments:
#             # Disconnect entity from config entry. This will cause the corresponding
#             # entity to be removed, but it will remain in the entity registry in case
#             # Member becomes visible again. Or the user can decide to delete the entity
#             # registry entry.
#             reg_entry = self._update_entity_registry(member_id, cfg_id=None)
#             LOGGER.warning(
#                 "%s is no longer in any visible Circle", _member_str(reg_entry)
#             )
#             LOGGER.debug("%s is no longer visible", _member_str(reg_entry, member_id))

#         for member_id in make_assignments:
#             cfg_id = None
#             for member, circle_ids in mem_cids_per_status[member_id].values():
#                 if cfg_id := find_a_config(assignable_configs, circle_ids):
#                     break
#             if not cfg_id:
#                 continue

#             cfg_entry = self._configs[cfg_id].coordinator.config_entry
#             if member_id in registered_members:
#                 reg_entry = self._update_entity_registry(
#                     member_id, cfg_id=cfg_id, name=member.name
#                 )
#             else:
#                 reg_entry = self._entity_reg.async_get_or_create(
#                     Platform.DEVICE_TRACKER,
#                     DOMAIN,
#                     member_id,
#                     suggested_object_id=member.name,
#                     config_entry=cfg_entry,
#                     original_name=member.name,
#                 )
#             if not reg_entry.disabled:
#                 result[cfg_id][member_id] = member

#             LOGGER.debug(
#                 "%s assigned to account %s%s",
#                 _member_str(reg_entry, member_id),
#                 cfg_entry.title,
#                 ": disabled" if reg_entry.disabled else "",
#             )
#             # self._dump_result(result, short=True)

#         return result

#     def _reg_entry(self, member_id: MemberID) -> RegistryEntry:
#         """Return current Entity Registry entry for Member."""
#         return self._entity_reg.entities[
#             cast(
#                 str,
#                 self._entity_reg.async_get_entity_id(
#                     Platform.DEVICE_TRACKER, DOMAIN, member_id
#                 ),
#             )
#         ]

#     def _update_entity_registry(
#         self,
#         member_id: MemberID,
#         *,
#         cfg_id: str | None | UndefinedType = UNDEFINED,
#         name: str | UndefinedType = UNDEFINED,
#     ) -> RegistryEntry:
#         """Update Entity Registry entry for Member.

#         Returns new Entity Registry entry.
#         """
#         reg_entry = self._reg_entry(member_id)
#         if cfg_id is UNDEFINED or reg_entry.disabled_by is RegistryEntryDisabler.USER:
#             disable_by: RegistryEntryDisabler | UndefinedType | None = UNDEFINED
#         elif (
#             cfg_id
#             and self._configs[cfg_id].coordinator.config_entry.pref_disable_new_entities
#         ):
#             disable_by = RegistryEntryDisabler.INTEGRATION
#         else:
#             disable_by = None
#         return self._entity_reg.async_update_entity(
#             reg_entry.entity_id,
#             config_entry_id=cfg_id,
#             disabled_by=disable_by,
#             original_name=name,
#         )

#     def _dump_result(self, result, msg="", short=False):  # type: ignore[no-untyped-def]
#         if msg:
#             msg += ": "
#         msg += "result:"
#         if len(result):
#             for cfg_id, mems in result.items():
#                 cfg_entry = cast(
#                     ConfigEntry, self.hass.config_entries.async_get_entry(cfg_id)
#                 )
#                 msg += f"\n  {cfg_id} {cfg_entry.title}:"
#                 if len(mems):
#                     if short:
#                         msg += f" {{{', '.join(f'{mem_id}: {mem.name if mem else None}' for mem_id, mem in mems.items())}}}"
#                     else:
#                         for mem_id, mem in mems.items():
#                             msg += f"\n    {mem_id}:"
#                             msg += f"\n      {mem.name}"
#                             msg += f"\n      {mem.loc}"
#                             msg += f"\n      {mem.status.name}"
#                             msg += f"\n      {mem.err_msg}"
#                 else:
#                     msg += f" {mems}"
#         else:
#             msg += f" {result}"
#         LOGGER.info(msg)


# def _member_str(reg_entry: RegistryEntry, member_id: MemberID | None = None) -> str:
#     """Return a string identifying Member."""
#     name = cast(str, reg_entry.name or reg_entry.original_name)
#     entity_id = reg_entry.entity_id
#     if member_id:
#         return f"{name} ({member_id} -> {entity_id})"
#     return f"{name} ({entity_id})"
