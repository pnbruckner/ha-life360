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
from homeassistant.helpers.issue_registry import IssueSeverity, async_create_issue
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import helpers
from .const import (
    CIRCLE_UPDATE_INTERVAL,
    COMM_MAX_RETRIES,
    COMM_TIMEOUT,
    DOMAIN,
    UPDATE_INTERVAL,
)
from .helpers import (
    AccountID,
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
    mem_circles: dict[MemberID, set[CircleID]] = field(default_factory=dict)


@dataclass
class AccountData:
    """Data for a Life360 account."""

    session: ClientSession
    api: helpers.Life360
    # TODO: Make this list part of data (for binary sensors)???
    failed: asyncio.Event
    failed_task: asyncio.Task


class LoginRateLimitErrorResp(Enum):
    """Response to Login or RateLimited errors."""

    ERROR = auto()
    SILENT = auto()
    RETRY = auto()


class RequestError(Enum):
    """Request error type."""

    NOT_MODIFIED = auto()
    NO_DATA = auto()


class Life360DataUpdateCoordinator(DataUpdateCoordinator[Members]):
    """Life360 data update coordinator."""

    config_entry: ConfigEntry
    _first_refresh: bool = True
    _update_data_task: asyncio.Task | None = None
    _update_cm_data_unsub: CALLBACK_TYPE | None = None
    _update_cm_data_task: asyncio.Task | None = None

    def __init__(self, hass: HomeAssistant, store: Life360Store) -> None:
        """Initialize data update coordinator."""
        self._update_data_lock = asyncio.Lock()
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=UPDATE_INTERVAL)
        # always_update added in 2023.9.
        if hasattr(self, "always_update"):
            self.always_update = False
        self._store = store
        self._options = ConfigOptions.from_dict(self.config_entry.options)
        self._acct_data: dict[AccountID, AccountData] = {}
        self._create_acct_data(self._options.accounts)
        self.__cm_data = CircleMemberData()
        self._member_circle_data: dict[MemberID, dict[CircleID, MemberData]] = {}

        self.config_entry.async_on_unload(
            self.config_entry.add_update_listener(self._config_entry_updated)
        )
        self.config_entry.async_on_unload(self._update_cm_data_stop)

    async def _async_update_data(self) -> Members:
        """Fetch the latest data from the source with lock."""
        old_data = self.data if self.data is not None else Members()
        if self._update_data_task:
            _LOGGER.error("Multiple simultaneous updates not supported")
            return old_data

        self._update_data_task = cast(asyncio.Task, asyncio.current_task())
        try:
            async with self._update_data_lock:
                return await self._do_update_data()
        except asyncio.CancelledError:
            self._update_data_task.uncancel()
            return old_data
        finally:
            self._update_data_task = None

    async def _do_update_data(self) -> Members:
        """Fetch the latest data from the source."""
        # TODO: How to handle errors, especially per aid/api???
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

            elif old_member_data := (self.data and self.data.get(mid)):
                result[mid] = old_member_data

        return result

    async def _cm_data(self) -> CircleMemberData:
        """Return current Circle & Member data."""
        # TODO: Create a sensor that lists all the Circles and which Members are in each
        #       and eventually, the names of the Places in each.
        if self._first_refresh:
            if self._store.loaded_ok:
                self._load_cm_data()
            else:
                _LOGGER.warning(
                    "Could not load Circles & Members from storage"
                    "; will wait for data from server"
                )
            await self._update_cm_data_start(retry_first=False)
            self._first_refresh = False

        return self.__cm_data

    def _load_cm_data(self) -> None:
        """Load Circles & Members from storage."""
        circles = self._store.circles
        mem_circles: dict[MemberID, set[CircleID]] = {}
        for cid, circle_data in circles.items():
            for mid in circle_data.mids:
                mem_circles.setdefault(mid, set()).add(cid)
        self.__cm_data = CircleMemberData(circles, mem_circles)

    async def _update_cm_data_start(self, retry_first: bool = True) -> None:
        """Start periodic updating of Circles & Members data."""
        if not retry_first:
            await self._update_cm_data(dt_util.now(), retry=False)
            self.config_entry.async_create_background_task(
                self.hass,
                self._update_cm_data_start(),
                "Start Circle & Member updating",
            )
            return

        await self._update_cm_data(dt_util.now())
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
        if not self._update_cm_data_task:
            return
        self._update_cm_data_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._update_cm_data_task

    async def _update_cm_data(self, now: datetime, retry: bool = True) -> None:
        """Update Life360 Circles & Members seen from all enabled accounts."""
        # Guard against being called again while previous call is still in progress.
        if self._update_cm_data_task:
            _LOGGER.warning("Background Circle & Member update taking too long")
            return
        _LOGGER.debug("Begin updating Circles & Members")
        self._update_cm_data_task = cast(asyncio.Task, asyncio.current_task())
        cancelled = False
        try:
            await self._do_update_cm_data(retry)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            _LOGGER.debug(
                "Updating Circles & Members %stook %s",
                "(which was cancelled) " if cancelled else "",
                dt_util.now() - now,
            )
            self._update_cm_data_task = None

    async def _do_update_cm_data(self, retry: bool) -> None:
        """Update Life360 Circles & Members seen from all enabled accounts."""
        circle_errors = False
        circles: dict[CircleID, CircleData] = {}

        # Get Circles each account can see, keeping track of which accounts can see each
        # Circle, since a Circle can be seen by more than one account.
        raw_circles_list = await self._get_circles(retry)
        for aid, raw_circles in zip(self._acct_data, raw_circles_list):
            if isinstance(raw_circles, RequestError):
                circle_errors = True
                continue
            for raw_circle in raw_circles:
                if (cid := CircleID(raw_circle["id"])) not in circles:
                    circles[cid] = CircleData(raw_circle["name"])
                circles[cid].aids.add(aid)

        # Get Members in each Circle, keeping track of which Circles each Member is in,
        # since a Member can be in more than one Circle.
        mem_circles: dict[MemberID, set[CircleID]] = {}
        for cid, circle_data in circles.items():
            # TODO: Get Members for each Circle in parallel.
            # For each Circle, there may be more than one account that can see it, so
            # keep trying if for some reason an error occurs while trying to use one.
            for aid in circle_data.aids:
                raw_members = await self._request(
                    aid,
                    self._acct_data[aid].api.get_circle_members,
                    cid,
                    msg=f"while getting Members in {circle_data.name} Circle",
                )
                if not isinstance(raw_members, RequestError):
                    for raw_member in raw_members:
                        # TODO: Add Member name, too???
                        mid = MemberID(raw_member["id"])
                        circle_data.mids.add(mid)
                        mem_circles.setdefault(mid, set()).add(cid)
                    break

        # If there were any errors while getting Circles for each account, then retry
        # must have been False. Since we haven't yet received Circle data for all
        # enabled accounts, use any old information that is available to fill in the
        # gaps for now. E.g., we don't want to remove any Member entity until we're
        # absolutely sure they are no longer in any Circle visible from all enabled
        # accounts.
        if circle_errors:
            for cid, old_circle_data in self.__cm_data.circles.items():
                if cid in circles:
                    circles[cid].aids |= old_circle_data.aids
                else:
                    circles[cid] = old_circle_data
                    for mid in old_circle_data.mids:
                        mem_circles.setdefault(mid, set()).add(cid)

        self.__cm_data = CircleMemberData(circles, mem_circles)

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

    async def _get_circles(
        self,
        retry: bool,
    ) -> list[list[dict[str, str]] | RequestError]:
        """Get Circles for each AccountID."""
        lrle_resp = (
            LoginRateLimitErrorResp.RETRY if retry else LoginRateLimitErrorResp.SILENT
        )
        return await asyncio.gather(  # type: ignore[no-any-return]
            *(
                self.config_entry.async_create_background_task(
                    self.hass,
                    self._request(
                        aid,
                        acct_data.api.get_circles,
                        msg="while getting Circles",
                        lrle_resp=lrle_resp,
                    ),
                    f"Get Circles for {aid}",
                )
                for aid, acct_data in self._acct_data.items()
            )
        )

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
        for aid in circle_data.aids:
            raw_member = await self._request(
                aid,
                partial(
                    self._acct_data[aid].api.get_circle_member,
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

    # _requests = 0

    # TODO: Add some overall timeout to make sure rate limiting & retrying can't make it take forever???
    async def _request(
        self,
        aid: AccountID,
        target: Callable[[*_Ts], Coroutine[Any, Any, _R]],
        *args: *_Ts,
        msg: str,
        lrle_resp: LoginRateLimitErrorResp = LoginRateLimitErrorResp.ERROR,
    ) -> _R | RequestError:
        """Make a request to the Life360 server."""
        if self._acct_data[aid].failed.is_set():
            return RequestError.NO_DATA

        login_errors = 0
        failed_task = self._acct_data[aid].failed_task
        while True:
            request_task = self.config_entry.async_create_background_task(
                self.hass,
                target(*args),
                f"Make request to {aid}",
            )
            done, _ = await asyncio.wait(
                [failed_task, request_task], return_when=asyncio.FIRST_COMPLETED
            )
            if failed_task in done:
                request_task.cancel()
                with suppress(asyncio.CancelledError, Life360Error):
                    await request_task
                return RequestError.NO_DATA

            try:
                # if aid == "federicktest95@gmail.com":
                #     self._requests += 1
                #     if self._requests == 1:
                #         request_task.cancel()
                #         raise LoginError("TEST TEST TEST")
                return await request_task
            except NotModified:
                return RequestError.NOT_MODIFIED
            except LoginError as exc:
                if lrle_resp is LoginRateLimitErrorResp.RETRY and login_errors < 4:
                    login_errors += 1
                    delay = 15 * 60
                    _LOGGER.debug(
                        "%s: login error %s: will retry in %i s", aid, msg, delay
                    )
                    await asyncio.sleep(delay)
                    continue
                level = (
                    logging.DEBUG
                    if lrle_resp is LoginRateLimitErrorResp.SILENT
                    else logging.ERROR
                )
                _LOGGER.log(level, "%s: login error %s: %s", aid, msg, exc)
                if lrle_resp is not LoginRateLimitErrorResp.SILENT:
                    self._handle_login_error(aid)
                return RequestError.NO_DATA
            except Life360Error as exc:
                rate_limited = isinstance(exc, RateLimited)
                if lrle_resp is LoginRateLimitErrorResp.RETRY and rate_limited:
                    delay = ceil(exc.retry_after or 0) + 10
                    _LOGGER.debug(
                        "%s: rate limited %s: will retry in %i s",
                        aid,
                        msg,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                # TODO: Keep track of errors per aid so we don't flood log???
                #       Maybe like DataUpdateCoordinator does it?
                level = (
                    logging.DEBUG
                    if rate_limited and lrle_resp is LoginRateLimitErrorResp.SILENT
                    else logging.ERROR
                )
                _LOGGER.log(level, "%s: %s: %s", aid, msg, exc)
                return RequestError.NO_DATA

    def _handle_login_error(self, aid: AccountID) -> None:
        """Handle account login error."""
        if (failed := self._acct_data[aid].failed).is_set():
            return
        # Signal all current requests using account to stop and return NO_DATA.
        failed.set()

        # Create repair issue for account and disable it. Deleting repair issues will be
        # handled by config flow.
        async_create_issue(
            self.hass,
            DOMAIN,
            aid,
            is_fixable=False,
            is_persistent=True,
            severity=IssueSeverity.ERROR,
            translation_key="login_error",
            translation_placeholders={"acct_id": aid},
        )
        options = self._options.as_dict()
        options["accounts"][aid]["enabled"] = False
        self.hass.config_entries.async_update_entry(self.config_entry, options=options)

    async def _config_entry_updated(self, _: HomeAssistant, entry: ConfigEntry) -> None:
        """Run when the config entry has been updated."""
        if self._options == (new_options := ConfigOptions.from_dict(entry.options)):
            return

        old_options = self._options
        self._options = new_options
        # Get previously and currently enabled accounts.
        old_accts = {
            aid: acct for aid, acct in old_options.accounts.items() if acct.enabled
        }
        new_accts = {
            aid: acct for aid, acct in new_options.accounts.items() if acct.enabled
        }
        if old_accts == new_accts and old_options.verbosity == new_options.verbosity:
            return

        old_acct_ids = set(old_accts)
        new_acct_ids = set(new_accts)

        for aid in old_acct_ids & new_acct_ids:
            api = self._acct_data[aid].api
            api.verbosity = new_options.verbosity
            # TODO: Change API to make authorization attr directly accessible.
            api._authorization = new_options.accounts[aid].authorization

        if old_accts == new_accts:
            return

        # Stop everything. Note that if _async_update_data gets cancelled, it will still
        # be scheduled to run again, so that does not need to be done here.
        await self._update_cm_data_stop()
        if update_data_task := self._update_data_task:
            update_data_task.cancel()

        # Prevent _do_update_data from running while _acct_data & __cm_data are being
        # updated.
        async with self._update_data_lock:
            if update_data_task:
                await asyncio.wait([update_data_task])

            del_acct_ids = old_acct_ids - new_acct_ids
            self._delete_acct_data(del_acct_ids)
            self._create_acct_data(new_acct_ids - old_acct_ids)

            # Remove any accounts that no longer exist, or at least, are no longer
            # enabled, from CircleMemberData. If that leaves any Circles with no
            # accounts that can access it, then also remove those Circles from
            # CircleMemberData. And, lastly, if that leaves any Members not associated
            # with at least one Circle, then remove those Members, too.
            no_aids: list[CircleID] = []
            for cid, circle_data in self.__cm_data.circles.items():
                circle_data.aids -= del_acct_ids
                if not circle_data.aids:
                    no_aids.append(cid)
            no_circles: list[MemberID] = []
            for cid in no_aids:
                del self.__cm_data.circles[cid]
                for mid, mem_circles in self.__cm_data.mem_circles.items():
                    mem_circles.discard(cid)
                    if not mem_circles:
                        no_circles.append(mid)
            for mid in no_circles:
                del self.__cm_data.mem_circles[mid]

            await self._update_cm_data_start()

    def _create_acct_data(self, aids: Iterable[AccountID]) -> None:
        """Create data needed for each specified Life360 account."""
        for aid in aids:
            acct = self._options.accounts[aid]
            if not acct.enabled:
                continue
            session = async_create_clientsession(self.hass, timeout=COMM_TIMEOUT)
            api = helpers.Life360(
                session,
                COMM_MAX_RETRIES,
                acct.authorization,
                verbosity=self._options.verbosity,
            )
            failed = asyncio.Event()
            failed_task = self.config_entry.async_create_background_task(
                self.hass,
                failed.wait(),
                f"Monitor failed requests to {aid}",
            )
            self._acct_data[aid] = AccountData(session, api, failed, failed_task)

    def _delete_acct_data(self, aids: Iterable[AccountID]) -> None:
        """Delete data previously created for each specified Life360 account."""
        for aid in aids:
            acct = self._acct_data.pop(aid)
            acct.session.detach()
            acct.failed_task.cancel()
