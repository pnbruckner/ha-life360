"""DataUpdateCoordinator for the Life360 integration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Iterable
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from functools import partial
import logging
from math import ceil
from typing import Any, TypeVar, TypeVarTuple, cast

from aiohttp import ClientSession
from life360 import Life360Error, LoginError, NotModified, RateLimited

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from . import helpers
from .const import (
    CIRCLE_UPDATE_INTERVAL,
    COMM_MAX_RETRIES,
    COMM_TIMEOUT,
    DOMAIN,
    UPDATE_INTERVAL,
)
from .helpers import (
    CircleData,
    CircleID,
    ConfigOptions,
    Life360Store,
    MemberData,
    MemberID,
    Members,
)

_LOGGER = logging.getLogger(__name__)

_R = TypeVar("_R")
_Ts = TypeVarTuple("_Ts")


@dataclass
class CircleMemberData:
    """Circle & Member data."""

    circles: dict[CircleID, CircleData] = field(default_factory=dict)
    # TODO: Include Member name somewhere, too???
    mem_circles: dict[MemberID, list[CircleID]] = field(default_factory=dict)


@dataclass
class AccountData:
    """Data for a Life360 account."""

    session: ClientSession
    api: helpers.Life360


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


class Life360DataUpdateCoordinator(DataUpdateCoordinator[Members]):
    """Life360 data update coordinator."""

    config_entry: ConfigEntry
    __cm_data: CircleMemberData | None = None
    _update_cm_data_unsub: CALLBACK_TYPE | None = None
    _update_cm_data_task: asyncio.Task | None = None

    def __init__(self, hass: HomeAssistant, store: Life360Store) -> None:
        """Initialize data update coordinator."""
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=UPDATE_INTERVAL)
        # always_update added in 2023.9.
        if hasattr(self, "always_update"):
            self.always_update = False
        self._store = store
        self._options = ConfigOptions.from_dict(self.config_entry.options)
        self._acct_data: dict[str, AccountData] = {}
        self._create_acct_data(self._options.accounts)
        # TODO: Make this list part of data (for binary sensors)???
        self._login_error: list[str] = []
        self._member_circle_data: dict[MemberID, dict[CircleID, MemberData]] = {}
        self._update_lock = asyncio.Lock()

        self.config_entry.async_on_unload(
            self.config_entry.add_update_listener(self._async_config_entry_updated)
        )
        self.config_entry.async_on_unload(self._update_cm_data_stop)

    async def _async_update_data(self) -> Members:
        """Fetch the latest data from the source while holding lock."""
        async with self._update_lock:
            return await self._do_update()

    async def _do_update(self) -> Members:
        """Fetch the latest data from the source."""
        # TODO: How to handle errors, especially per username/api???
        result = Members()

        cm_data = await self._cm_data()
        n_circles = len(cm_data.circles)
        raw_member_list_list = await asyncio.gather(
            *(self._get_raw_member_list(mid, cm_data) for mid in cm_data.mem_circles)
        )
        for mid, raw_member_list in zip(cm_data.mem_circles, raw_member_list_list):
            cids = cm_data.mem_circles[mid]
            old_mcd = self._member_circle_data.get(mid)
            member_circle_data: dict[CircleID, MemberData] = {}
            for cid, raw_member in zip(cids, raw_member_list):
                if not isinstance(raw_member, RequestError):
                    member_circle_data[cid] = MemberData.from_server(raw_member)
                elif old_mcd and (old_cd := old_mcd.get(cid)):
                    member_circle_data[cid] = old_cd
            if member_circle_data:
                self._member_circle_data[mid] = member_circle_data
                result[mid] = sorted(member_circle_data.values())[-1]
                if n_circles > 1:
                    # Each Circle has its own Places. Collect all the Places where the
                    # Member might be, while keeping the Circle they came from. Then
                    # update the chosen MemberData with the Place or Places where the
                    # Member is, with each having a suffix of the name of its Circle.
                    places = {
                        cid: cast(str, member_data.loc.details.place)
                        for cid, member_data in member_circle_data.items()
                        if member_data.loc and member_data.loc.details.place
                    }
                    if places:
                        place: str | list[str] = [
                            f"{c_place} ({cm_data.circles[cid].name})"
                            for cid, c_place in places.items()
                        ]
                        if len(place) == 1:
                            place = place[0]
                        member_data = deepcopy(result[mid])
                        assert member_data.loc
                        member_data.loc.details.place = place
                        result[mid] = member_data

            elif old_member_data := self.data.get(mid):
                result[mid] = old_member_data

        return result

    async def _cm_data(self) -> CircleMemberData:
        """Return current Circle & Member data."""
        # TODO: Create a sensor that lists all the Circles and which Members are in each
        #       and eventually, the names of the Places in each.
        if not self.__cm_data:
            # Try to get Circles & Members from storage.
            self.__cm_data = self._load_cm_data()
            run_now = False
            if not self.__cm_data:
                # Get Circles & Members from server, returning immediately with whatever
                # data is available.
                self.__cm_data, run_now = await self._get_cm_data(at_startup=True)

            # eager_start was added in 2024.3.
            start_updating = partial(
                self.config_entry.async_create_background_task,
                self.hass,
                self._update_cm_data_start(run_now=run_now),
                "Start periodic Circle & Member updating",
            )
            try:
                start_updating(eager_start=True)
            except TypeError:
                start_updating()

        return self.__cm_data

    def _load_cm_data(self) -> CircleMemberData | None:
        """Load Circles & Members from storage."""
        if not self._store.loaded_ok:
            _LOGGER.warning(
                "Could not load Circles & Members from storage"
                "; will use whatever data is immediately available from server"
            )
            return None

        circles = self._store.circles
        mem_circles: dict[MemberID, list[CircleID]] = {}
        for cid, circle_data in circles.items():
            for mid in circle_data.mids:
                mem_circles.setdefault(mid, []).append(cid)
        return CircleMemberData(circles, mem_circles)

    async def _get_cm_data(self, at_startup: bool) -> tuple[CircleMemberData, bool]:
        """Get Life360 Circles & Members seen from all enabled accounts.

        If at_startup is True and any requests are rate limited, don't wait, just return
        what is currently available.

        If at_startup is False and any requests are rate limited, wait as indicated for
        each and return when all are done.

        Returns CircleMemberData and a bool that is True if at_startup is True and any
        requests were rate limited.
        """
        if self.__cm_data:
            old_circles = self.__cm_data.circles
        else:
            old_circles = {}
        usernames = list(self._acct_data)

        circles: dict[CircleID, CircleData] = {}
        raw_circles_list = await self._get_circles(usernames, at_startup)
        rate_limited = False
        for username, raw_circles in zip(usernames, raw_circles_list):
            if raw_circles is RequestError.NOT_MODIFIED:
                for cid, circle_data in old_circles.items():
                    if username not in circle_data.usernames:
                        continue
                    if cid not in circles:
                        circles[cid] = CircleData(circle_data.name)
                    circles[cid].usernames.append(username)
            elif isinstance(raw_circles, RequestError):
                if raw_circles is RequestError.RATE_LIMITED:
                    rate_limited = True
            else:
                for raw_circle in raw_circles:
                    if (cid := CircleID(raw_circle["id"])) not in circles:
                        circles[cid] = CircleData(raw_circle["name"])
                    circles[cid].usernames.append(username)

        mem_circles: dict[MemberID, list[CircleID]] = {}
        for cid, circle_data in circles.items():
            for username in circle_data.usernames:
                raw_members = await self._request(
                    username,
                    self._acct_data[username].api.get_circle_members,
                    cid,
                    msg=f"while getting Members in {circle_data.name} Circle",
                )
                if not isinstance(raw_members, RequestError):
                    for raw_member in raw_members:
                        # TODO: Add Member name, too???
                        mid = MemberID(raw_member["id"])
                        circle_data.mids.append(mid)
                        mem_circles.setdefault(mid, []).append(cid)
                    break

        # Protect storage writing in case we get cancelled while it's running. We do not
        # want to interrupt that process. It is an atomic operation, so if we get
        # cancelled and called again while it's running, and we somehow manage to get to
        # this point again while it still hasn't finished, we'll just wait until it is
        # done and it will be begun again with the new data.
        self._store.circles = circles
        save_task = self.config_entry.async_create_task(
            self.hass,
            self._store.save(),
            "Save to Life360 storage",
        )
        await asyncio.shield(save_task)

        return CircleMemberData(circles, mem_circles), rate_limited

    async def _get_circles(
        self, usernames: Iterable[str], at_startup: bool
    ) -> list[list[dict[str, str]] | RequestError]:
        """Get Circles for each username."""
        rla = RateLimitedAction.WARNING if at_startup else RateLimitedAction.RETRY
        return await asyncio.gather(  # type: ignore[no-any-return]
            *(
                self.config_entry.async_create_background_task(
                    self.hass,
                    self._request(
                        username,
                        partial(
                            self._acct_data[username].api.get_circles,
                            raise_not_modified=not at_startup,
                        ),
                        msg="while getting Circles",
                        rate_limited_action=rla,
                    ),
                    f"Get Circles for {username}",
                )
                for username in usernames
            )
        )

    async def _update_cm_data_start(self, run_now: bool) -> None:
        """Start periodic updating of Circles & Members data."""
        if run_now:
            self.__cm_data, _ = await self._get_cm_data(at_startup=False)
        self._update_cm_data_unsub = async_track_time_interval(
            self.hass, self._update_cm_data, CIRCLE_UPDATE_INTERVAL
        )

    async def _update_cm_data_stop(self) -> None:
        """Stop periodic updating of Circles & Members data."""
        # Stop running it periodically.
        if self._update_cm_data_unsub:
            self._update_cm_data_unsub()
            self._update_cm_data_unsub = None

        # Stop it, and wait for it to stop, if it is running now.
        if task := self._update_cm_data_task:
            self._update_cm_data_task = None
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _update_cm_data(self, now: datetime) -> None:
        """Update Circles & Members data."""
        # Guard against being called again while previous call is still in progress.
        if self._update_cm_data_task:
            return
        self._update_cm_data_task = asyncio.current_task()
        self.__cm_data, _ = await self._get_cm_data(at_startup=False)
        self._update_cm_data_task = None

    async def _get_raw_member_list(
        self, mid: MemberID, cm_data: CircleMemberData
    ) -> list[dict[str, Any] | RequestError]:
        """Get raw Member data from each Circle Member is in."""
        tasks: list[asyncio.Task[dict[str, Any] | RequestError]] = []
        for cid in cm_data.mem_circles[mid]:
            circle_data = cm_data.circles[cid]
            tasks.append(
                self.config_entry.async_create_background_task(
                    self.hass,
                    self._get_raw_member(mid, cid, circle_data),
                    f"Get Member from {circle_data.name} Circle",
                )
            )
        return await asyncio.gather(*tasks)

    async def _get_raw_member(
        self, mid: MemberID, cid: CircleID, circle_data: CircleData
    ) -> dict[str, Any] | RequestError:
        """Get raw Member data from given Circle."""
        for username in circle_data.usernames:
            raw_member = await self._request(
                username,
                partial(
                    self._acct_data[username].api.get_circle_member,
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
        username: str,
        target: Callable[[*_Ts], Coroutine[Any, Any, _R]],
        *args: *_Ts,
        msg: str | None = None,
        rate_limited_action: RateLimitedAction = RateLimitedAction.ERROR,
    ) -> _R | RequestError:
        """Make a request to the Life360 server."""
        if username in self._login_error:
            return RequestError.NO_DATA

        if not msg:
            msg = "while making a server request"
        while True:
            try:
                return await target(*args)
            except NotModified:
                return RequestError.NOT_MODIFIED
            except LoginError as exc:
                _LOGGER.error("%s: login error %s: %s", username, msg, exc)
                await self._handle_login_error(username)
                return RequestError.NO_DATA
            except Life360Error as exc:
                level = logging.ERROR
                result = RequestError.NO_DATA
                if isinstance(exc, RateLimited):
                    if rate_limited_action is RateLimitedAction.RETRY:
                        delay = ceil(exc.retry_after or 0) + 10
                        _LOGGER.debug(
                            "%s: rate limited %s: will retry in %i s",
                            username,
                            msg,
                            delay,
                        )
                        await asyncio.sleep(delay)
                        continue
                    if rate_limited_action is RateLimitedAction.WARNING:
                        level = logging.WARNING
                    result = RequestError.RATE_LIMITED
                # TODO: Keep track of errors per username so we don't flood log???
                #       Maybe like DataUpdateCoordinator does it?
                _LOGGER.log(level, "%s: %s: %s", username, msg, exc)
                return result

    async def _handle_login_error(self, username: str) -> None:
        """Handle account login error."""
        self._login_error.append(username)
        # TODO: Log repair issue.
        # TODO: How to "reactivate" account (i.e., remove from self._login_error)???

    def _create_acct_data(self, acct_ids: Iterable[str]) -> None:
        """Create Life360 API objects for accounts."""
        for acct_id in acct_ids:
            acct = self._options.accounts[acct_id]
            if not acct.enabled:
                continue
            session = async_create_clientsession(self.hass, timeout=COMM_TIMEOUT)
            api = helpers.Life360(
                session,
                COMM_MAX_RETRIES,
                acct.authorization,
                verbosity=self._options.verbosity,
            )
            self._acct_data[acct_id] = AccountData(session, api)

    async def _async_config_entry_updated(
        self, _: HomeAssistant, entry: ConfigEntry
    ) -> None:
        """Run when the config entry has been updated."""
        if self._options == (new_options := ConfigOptions.from_dict(entry.options)):
            return

        old_options = self._options
        self._options = new_options

        old_accts = {
            acct_id: acct
            for acct_id, acct in old_options.accounts.items()
            if acct.enabled
        }
        new_accts = {
            acct_id: acct
            for acct_id, acct in new_options.accounts.items()
            if acct.enabled
        }
        if old_accts == new_accts and old_options.verbosity == new_options.verbosity:
            return

        await self._update_cm_data_stop()

        old_acct_ids = set(old_accts)
        new_acct_ids = set(new_accts)

        async with self._update_lock:
            for acct_id in old_acct_ids - new_acct_ids:
                self._acct_data.pop(acct_id).session.detach()
            self._create_acct_data(new_acct_ids - old_acct_ids)
            for acct_id in old_acct_ids & new_acct_ids:
                api = self._acct_data[acct_id].api
                api.verbosity = new_options.verbosity
                # TODO: Change API to make authorization attr directly accessible.
                api._authorization = new_options.accounts[acct_id].authorization

        await self._update_cm_data_start(run_now=True)
