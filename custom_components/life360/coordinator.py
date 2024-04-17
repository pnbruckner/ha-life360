"""DataUpdateCoordinator for the Life360 integration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from functools import partial
import logging
from math import ceil
from typing import Any, TypeVar, TypeVarTuple

from life360 import Life360Error, LoginError, NotModified, RateLimited

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
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
    _updating_cm_data: bool = False

    def __init__(self, hass: HomeAssistant, store: Life360Store) -> None:
        """Initialize data update coordinator."""
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=UPDATE_INTERVAL)
        self._store = store
        # TODO: Update self.options & self._apis when config options change.
        options = ConfigOptions.from_dict(self.config_entry.options)
        self._apis = {
            username: helpers.Life360(
                async_create_clientsession(hass, timeout=COMM_TIMEOUT),
                COMM_MAX_RETRIES,
                acct.authorization,
                verbosity=options.verbosity,
            )
            for username, acct in options.accounts.items()
            if acct.enabled
        }
        # TODO: Make this list part of data (for binary sensors)???
        self._login_error: list[str] = []
        self._member_circle_data: dict[MemberID, dict[CircleID, MemberData]] = {}

    async def _async_update_data(self) -> Members:
        """Fetch the latest data from the source."""
        # TODO: How to handle errors, especially per username/api???
        result = Members()

        cm_data = await self._cm_data()
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
            elif old_member_data := self.data.get(mid):
                result[mid] = old_member_data

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

        Returns CircleMemberData and a bool that is True if any requests were rate
        limited.
        """
        if self.__cm_data:
            old_circles = self.__cm_data.circles
        else:
            old_circles = {}
        usernames = list(self._apis)

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
                    self._apis[username].get_circle_members,
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

        self._store.circles = circles
        await self._store.save()

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
                            self._apis[username].get_circles,
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
                    self._apis[username].get_circle_member,
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
