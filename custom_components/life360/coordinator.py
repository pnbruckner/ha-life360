"""DataUpdateCoordinator for the Life360 integration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Iterable
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum, auto
from functools import partial
import logging
from math import ceil
from typing import Any, TypeVar, TypeVarTuple, cast

from aiohttp import ClientSession
from life360 import Life360Error, LoginError, NotFound, NotModified, RateLimited

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.issue_registry import IssueSeverity, async_create_issue
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import helpers
from .const import (
    COMM_MAX_RETRIES,
    COMM_TIMEOUT,
    DOMAIN,
    LOGIN_ERROR_RETRY_DELAY,
    LTD_LOGIN_ERROR_RETRY_DELAY,
    MAX_LTD_LOGIN_ERROR_RETRIES,
    SIGNAL_ACCT_STATUS,
    UPDATE_INTERVAL,
)
from .helpers import (
    AccountID,
    CircleData,
    CircleID,
    CirclesMembersData,
    ConfigOptions,
    Life360Store,
    MemberData,
    MemberDetails,
    MemberID,
    NoLocReason,
)

_LOGGER = logging.getLogger(__name__)

_R = TypeVar("_R")
_Ts = TypeVarTuple("_Ts")


@dataclass
class AccountData:
    """Data for a Life360 account."""

    session: ClientSession
    api: helpers.Life360
    failed: asyncio.Event
    failed_task: asyncio.Task
    online: bool = True


class LoginRateLimitErrResp(Enum):
    """Response to Login or RateLimited errors."""

    LTD_LOGIN_ERROR_RETRY = auto()
    RETRY = auto()
    SILENT = auto()


class RequestError(Enum):
    """Request error type."""

    NOT_FOUND = auto()
    NOT_MODIFIED = auto()
    NO_DATA = auto()


class CirclesMembersDataUpdateCoordinator(DataUpdateCoordinator[CirclesMembersData]):
    """Circles & Members data update coordinator."""

    config_entry: ConfigEntry
    _bg_update_task: asyncio.Task | None = None
    _fg_update_task: asyncio.Task | None = None

    def __init__(self, hass: HomeAssistant, store: Life360Store) -> None:
        """Initialize data update coordinator."""
        super().__init__(hass, _LOGGER, name="Circles & Members")
        self._store = store
        self.data = self._data_from_store()
        self._options = ConfigOptions.from_dict(self.config_entry.options)
        self._acct_data: dict[AccountID, AccountData] = {}
        self._create_acct_data(self._options.accounts)
        self._client_request_ok = asyncio.Event()
        self._client_request_ok.set()
        self._client_tasks: set[asyncio.Task] = set()

        self.config_entry.async_on_unload(
            self.config_entry.add_update_listener(self._config_entry_updated)
        )

    def acct_online(self, aid: AccountID) -> bool:
        """Return if account is online."""
        # When config updates and there's a new, enabled account, binary sensor could
        # get created before coordinator finishes updating from the same event. In that
        # case, just return True. If/when the account is determined to be offline, the
        # binary sensor will be updated accordingly.
        if aid not in self._acct_data:
            return True
        return self._acct_data[aid].online

    # Once supporting only HA 2024.5 or newer, change to @cached_property and clear
    # cache (i.e., if hasattr(self, "mem_circles"): delattr(self, "mem_circles"))
    # in _async_refresh_finished override, after call to async_set_updated_data and in
    # _config_entry_updated after updating self.data.circles.
    @property
    def mem_circles(self) -> dict[MemberID, set[CircleID]]:
        """Return Circles Members are in."""
        return {
            mid: {
                cid
                for cid, circle_data in self.data.circles.items()
                if mid in circle_data.mids
            }
            for mid in self.data.mem_details
        }

    async def update_member_location(self, mid: MemberID) -> None:
        """Request Member location update."""
        # Member may no longer be available before corresponding device_tracker entity
        # has been removed.
        if mid not in self.data.mem_details:
            return
        name = self.data.mem_details[mid].name
        # Member may be in more than one Circle, and each of those Circles might be
        # accessible from more than one account. So try each Circle/account combination
        # until one works.
        for cid in self.mem_circles[mid]:
            circle_data = self.data.circles[cid]
            for aid in circle_data.aids:
                api = self._acct_data[aid].api
                result = await self._client_request(
                    aid,
                    api.request_circle_member_location_update,
                    cid,
                    mid,
                    msg=(
                        f"while requesting location update for {name} "
                        f"via {circle_data.name} Circle"
                    ),
                )
                if not isinstance(result, RequestError):
                    return

        _LOGGER.error("Could not update location of %s", name)

    async def get_raw_member_data(
        self, mid: MemberID
    ) -> dict[CircleID, dict[str, Any] | RequestError] | None:
        """Get raw Member data from each Circle Member is in."""
        # Member may no longer be available before corresponding device_tracker entity
        # has been removed.
        if mid not in self.data.mem_details:
            return None
        cids = self.mem_circles[mid]
        raw_member_list = await asyncio.gather(
            *(self._get_raw_member(mid, cid) for cid in cids)
        )
        return dict(zip(cids, raw_member_list, strict=True))

    def _data_from_store(self) -> CirclesMembersData:
        """Get Circles & Members from storage."""
        if not self._store.loaded_ok:
            _LOGGER.warning(
                "Could not load Circles & Members from storage"
                "; will wait for data from server"
            )
            return CirclesMembersData()
        return CirclesMembersData(self._store.circles, self._store.mem_details)

    async def _async_update_data(self) -> CirclesMembersData:
        """Fetch the latest data from the source."""
        done_msg = "Circles & Members list retrieval %s"
        assert not self._fg_update_task
        self._fg_update_task = asyncio.current_task()
        try:
            data, complete = await self._update_data(retry=False)
            if not complete:
                _LOGGER.warning(
                    "Could not retrieve full Circles & Members list from server"
                    "; will retry"
                )

                async def bg_update() -> None:
                    """Update Circles & Members in background."""
                    try:
                        data, _ = await self._update_data(retry=True)
                        self.async_set_updated_data(data)
                        _LOGGER.warning(done_msg, "complete")
                    except asyncio.CancelledError:
                        _LOGGER.warning(done_msg, "cancelled")
                        raise
                    finally:
                        self._bg_update_task = None

                assert not self._bg_update_task
                self._bg_update_task = self.config_entry.async_create_background_task(
                    self.hass, bg_update(), "Circles & Members background update"
                )

            elif not self._store.loaded_ok:
                _LOGGER.warning(done_msg, "complete")

            return data  # noqa: TRY300
        except asyncio.CancelledError:
            _LOGGER.warning(done_msg, "cancelled")
            raise
        finally:
            self._fg_update_task = None

    async def _update_data(self, retry: bool) -> tuple[CirclesMembersData, bool]:
        """Update Life360 Circles & Members seen from all enabled accounts."""
        start = dt_util.utcnow()
        _LOGGER.debug("Begin updating Circles & Members")
        cancelled = False
        try:
            return await self._do_update(retry)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            _LOGGER.debug(
                "Updating Circles & Members %stook %s",
                "(which was cancelled) " if cancelled else "",
                dt_util.utcnow() - start,
            )

    async def _do_update(self, retry: bool) -> tuple[CirclesMembersData, bool]:
        """Update Life360 Circles & Members seen from all enabled accounts.

        rerty: If True, will retry indefinitely if login or rate limiting errors occur.
        If False, will retrieve whatever data it can without retrying login or rate
        limiting errors.

        Returns True if Circles & Members were retrieved from all accounts without
        error, or False if retry was False and at least one error occurred.
        """
        circle_errors = False
        circles: dict[CircleID, CircleData] = {}

        # Get Circles each account can see, keeping track of which accounts can see each
        # Circle, since a Circle can be seen by more than one account.
        raw_circles_list = await self._get_raw_circles_list(retry)
        for aid, raw_circles in zip(self._acct_data, raw_circles_list, strict=True):
            if isinstance(raw_circles, RequestError):
                circle_errors = True
                continue
            for raw_circle in raw_circles:
                if (cid := CircleID(raw_circle["id"])) not in circles:
                    circles[cid] = CircleData(raw_circle["name"])
                circles[cid].aids.add(aid)

        # Get Members in each Circle, recording their name & entity_picture.
        mem_details: dict[MemberID, MemberDetails] = {}
        raw_members_list = await self._get_raw_members_list(circles)
        for circle, raw_members in zip(circles.items(), raw_members_list, strict=True):
            if not isinstance(raw_members, RequestError):
                cid, circle_data = circle
                for raw_member in raw_members:
                    mid = MemberID(raw_member["id"])
                    circle_data.mids.add(mid)
                    if mid not in mem_details:
                        mem_details[mid] = MemberDetails.from_server(raw_member)

        # If there were any errors while getting Circles for each account, then retry
        # must have been False. Since we haven't yet received Circle data for all
        # enabled accounts, use any old information that is available to fill in the
        # gaps for now. E.g., we don't want to remove any Member entity until we're
        # absolutely sure they are no longer in any Circle visible from all enabled
        # accounts.
        if circle_errors:
            for cid, old_circle_data in self.data.circles.items():
                if cid in circles:
                    circles[cid].aids |= old_circle_data.aids
                else:
                    circles[cid] = old_circle_data
            for mid, old_md in self.data.mem_details.items():
                if mid not in mem_details:
                    mem_details[mid] = old_md

        # Protect storage writing in case we get cancelled while it's running. We do not
        # want to interrupt that process. It is an atomic operation, so if we get
        # cancelled and called again while it's running, and we somehow manage to get to
        # this point again while it still hasn't finished, we'll just wait until it is
        # done and it will be begun again with the new data.
        self._store.circles = circles
        self._store.mem_details = mem_details
        save_task = self.config_entry.async_create_task(
            self.hass,
            self._store.save(),
            "Save to Life360 storage",
        )
        await asyncio.shield(save_task)

        return CirclesMembersData(circles, mem_details), not circle_errors

    async def _get_raw_circles_list(
        self,
        retry: bool,
    ) -> list[list[dict[str, str]] | RequestError]:
        """Get raw Circle data for each Circle that can be seen by each account."""
        lrle_resp = (
            LoginRateLimitErrResp.RETRY if retry else LoginRateLimitErrResp.SILENT
        )
        return await asyncio.gather(  # type: ignore[no-any-return]
            *(
                self._request(
                    aid,
                    acct_data.api.get_circles,
                    msg="while getting Circles",
                    lrle_resp=lrle_resp,
                )
                for aid, acct_data in self._acct_data.items()
            )
        )

    async def _get_raw_members_list(
        self, circles: dict[CircleID, CircleData]
    ) -> list[list[dict[str, Any]] | RequestError]:
        """Get raw Member data for each Member in each Circle."""

        async def get_raw_members(
            cid: CircleID, circle_data: CircleData
        ) -> list[dict[str, Any]] | RequestError:
            """Get raw Member data for each Member in Circle."""
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
                    return raw_members  # type: ignore[no-any-return]
            # TODO: It's possible Circle was deleted, or accounts were removed from
            #       Circle, after the Circles list was obtained. This is very unlikely,
            #       and this is not called very often, so for now, don't worry about it.
            #       To be really robust, this possibility should be handled.
            return RequestError.NO_DATA

        return await asyncio.gather(
            *(get_raw_members(cid, circle_data) for cid, circle_data in circles.items())
        )

    async def _get_raw_member(
        self, mid: MemberID, cid: CircleID
    ) -> dict[str, Any] | RequestError:
        """Get raw Member data from given Circle."""
        name = self.data.mem_details[mid].name
        circle_data = self.data.circles[cid]
        raw_member: dict[str, Any] | RequestError = RequestError.NO_DATA
        for aid in circle_data.aids:
            raw_member = await self._client_request(
                aid,
                partial(
                    self._acct_data[aid].api.get_circle_member,
                    cid,
                    mid,
                    raise_not_modified=True,
                ),
                msg=f"while getting data for {name} from {circle_data.name} Circle",
            )
            if raw_member is RequestError.NOT_MODIFIED:
                return RequestError.NOT_MODIFIED
            if not isinstance(raw_member, RequestError):
                return raw_member
        # Can be NO_DATA or NOT_FOUND.
        return raw_member

    async def _client_request(
        self,
        aid: AccountID,
        target: Callable[[*_Ts], Coroutine[Any, Any, _R]],
        *args: *_Ts,
        msg: str,
    ) -> _R | RequestError:
        """Make a request to the Life360 server on behalf of Member coordinator."""
        await self._client_request_ok.wait()

        task = self.config_entry.async_create_background_task(
            self.hass,
            self._request(aid, target, *args, msg=msg),
            f"Make client request to {aid}",
        )
        self._client_tasks.add(task)
        try:
            return await task
        except asyncio.CancelledError:
            return RequestError.NO_DATA
        finally:
            self._client_tasks.discard(task)

    # _requests = 0

    async def _request(
        self,
        aid: AccountID,
        target: Callable[[*_Ts], Coroutine[Any, Any, _R]],
        *args: *_Ts,
        msg: str,
        lrle_resp: LoginRateLimitErrResp = LoginRateLimitErrResp.LTD_LOGIN_ERROR_RETRY,
    ) -> _R | RequestError:
        """Make a request to the Life360 server."""
        if self._acct_data[aid].failed.is_set():
            return RequestError.NO_DATA

        start = dt_util.utcnow()
        login_error_retries = 0
        delay: int | None = None
        delay_reason = ""
        warned = False

        failed_task = self._acct_data[aid].failed_task
        request_task: asyncio.Task[_R] | None = None
        try:
            while True:
                if delay is not None:
                    if (
                        not warned
                        and (dt_util.utcnow() - start).total_seconds() + delay > 60 * 60
                    ):
                        _LOGGER.warning(
                            "Getting response from Life360 for %s "
                            "is taking longer than expected",
                            aid,
                        )
                        warned = True
                    _LOGGER.debug(
                        "%s: %s %s: will retry (%i) in %i s",
                        aid,
                        delay_reason,
                        msg,
                        login_error_retries,
                        delay,
                    )
                    await asyncio.sleep(delay)
                request_task = self.config_entry.async_create_background_task(
                    self.hass,
                    target(*args),
                    f"Make request to {aid}",
                )
                done, _ = await asyncio.wait(
                    [failed_task, request_task], return_when=asyncio.FIRST_COMPLETED
                )
                if failed_task in done:
                    (rt := request_task).cancel()
                    request_task = None
                    with suppress(asyncio.CancelledError, Life360Error):
                        await rt
                    return RequestError.NO_DATA

                try:
                    # if aid == "federicktest95@gmail.com":
                    #     self._requests += 1
                    #     if self._requests == 1:
                    #         (rt := request_task).cancel()
                    #         request_task = None
                    #         with suppress(BaseException):
                    #             await rt
                    #         raise LoginError("TEST TEST TEST")
                    result = await request_task

                except NotFound:
                    self._set_acct_exc(aid)
                    return RequestError.NOT_FOUND

                except NotModified:
                    self._set_acct_exc(aid)
                    return RequestError.NOT_MODIFIED

                except LoginError as exc:
                    self._acct_data[aid].session.cookie_jar.clear()

                    if (
                        lrle_resp is LoginRateLimitErrResp.RETRY
                        or lrle_resp is LoginRateLimitErrResp.LTD_LOGIN_ERROR_RETRY
                        and login_error_retries < MAX_LTD_LOGIN_ERROR_RETRIES
                    ):
                        self._set_acct_exc(aid)
                        if lrle_resp is LoginRateLimitErrResp.RETRY:
                            delay = LOGIN_ERROR_RETRY_DELAY
                        else:
                            delay = LTD_LOGIN_ERROR_RETRY_DELAY
                        delay_reason = "login error"
                        login_error_retries += 1
                        continue

                    treat_as_error = lrle_resp is not LoginRateLimitErrResp.SILENT
                    self._set_acct_exc(aid, not treat_as_error, msg, exc)
                    if treat_as_error:
                        self._handle_login_error(aid)
                    return RequestError.NO_DATA

                except Life360Error as exc:
                    rate_limited = isinstance(exc, RateLimited)
                    if lrle_resp is LoginRateLimitErrResp.RETRY and rate_limited:
                        self._set_acct_exc(aid)
                        delay = ceil(cast(RateLimited, exc).retry_after or 0) + 10
                        delay_reason = "rate limited"
                        continue

                    treat_as_error = not (
                        rate_limited and lrle_resp is LoginRateLimitErrResp.SILENT
                    )
                    self._set_acct_exc(aid, not treat_as_error, msg, exc)
                    return RequestError.NO_DATA

                else:
                    request_task = None
                    self._set_acct_exc(aid)
                    return result

        except asyncio.CancelledError:
            if request_task:
                request_task.cancel()
                with suppress(asyncio.CancelledError, Life360Error):
                    await request_task
            raise
        finally:
            if warned:
                _LOGGER.warning("Done trying to get response from Life360 for %s", aid)

    def _set_acct_exc(
        self,
        aid: AccountID,
        online: bool = True,
        msg: str = "",
        exc: Exception | None = None,
    ) -> None:
        """Set account exception status and signal clients if it has changed."""
        acct = self._acct_data[aid]
        if exc is not None:
            level = logging.ERROR if not online and acct.online else logging.DEBUG
            _LOGGER.log(level, "%s: %s: %s", aid, msg, exc)

        if online == acct.online:
            return

        if online and not acct.online:
            _LOGGER.error("%s: Fetching data recovered", aid)
        acct.online = online
        async_dispatcher_send(self.hass, SIGNAL_ACCT_STATUS, aid)

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
            api.authorization = new_options.accounts[aid].authorization
            api.name = (
                aid
                if new_options.verbosity >= 3
                else f"Account {list(self._acct_data).index(aid) + 1}"
            )
            api.verbosity = new_options.verbosity

        if old_accts == new_accts:
            return

        # Prevent any client requests from starting.
        self._client_request_ok.clear()

        # Stop everything.
        tasks = set(self._client_tasks)
        if self._fg_update_task:
            tasks.add(self._fg_update_task)
        if self._bg_update_task:
            tasks.add(self._bg_update_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._fg_update_task = None
        self._bg_update_task = None

        del_acct_ids = old_acct_ids - new_acct_ids
        self._delete_acct_data(del_acct_ids)
        self._create_acct_data(new_acct_ids - old_acct_ids)

        # Remove any accounts that no longer exist, or at least, are no longer
        # enabled. If that leaves any Circles with no accounts that can access it, then
        # also remove those Circles. And, lastly, if that leaves any Members not
        # associated with at least one Circle, then remove those Members, too.
        no_aids: list[CircleID] = []
        for cid, circle_data in self.data.circles.items():
            circle_data.aids -= del_acct_ids
            if not circle_data.aids:
                no_aids.append(cid)
        for cid in no_aids:
            del self.data.circles[cid]
        for mid in [mid for mid in self.data.mem_details if not self.mem_circles[mid]]:
            del self.data.mem_details[mid]

        await self.async_refresh()

        # Allow client requests to proceed.
        self._client_request_ok.set()

    def _create_acct_data(self, aids: Iterable[AccountID]) -> None:
        """Create data needed for each specified Life360 account."""
        for idx, aid in enumerate(aids):
            acct = self._options.accounts[aid]
            if not acct.enabled:
                continue
            session = async_create_clientsession(self.hass, timeout=COMM_TIMEOUT)
            name = aid if self._options.verbosity >= 3 else f"Account {idx + 1}"
            api = helpers.Life360(
                session,
                COMM_MAX_RETRIES,
                acct.authorization,
                name=name,
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


class MemberDataUpdateCoordinator(DataUpdateCoordinator[MemberData]):
    """Member data update coordinator."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: CirclesMembersDataUpdateCoordinator,
        mid: MemberID,
    ) -> None:
        """Initialize data update coordinator."""
        mem_details = coordinator.data.mem_details[mid]
        super().__init__(
            hass, _LOGGER, name=mem_details.name, update_interval=UPDATE_INTERVAL
        )
        # always_update added in 2023.9.
        if hasattr(self, "always_update"):
            self.always_update = False
        self.data = MemberData(mem_details)
        self._coordinator = coordinator
        self._mid = mid
        self._member_data: dict[CircleID, MemberData] = {}

    async def update_location(self) -> None:
        """Request Member location update."""
        await self._coordinator.update_member_location(self._mid)

    async def _async_update_data(self) -> MemberData:
        """Fetch the latest data from the source."""
        raw_member_data = await self._coordinator.get_raw_member_data(self._mid)
        # Member may no longer be available, but we haven't been removed yet.
        if raw_member_data is None:
            return self.data

        member_data: dict[CircleID, MemberData] = {}
        for cid, raw_member in raw_member_data.items():
            if not isinstance(raw_member, RequestError):
                member_data[cid] = MemberData.from_server(raw_member)
            elif raw_member is RequestError.NOT_FOUND:
                member_data[cid] = MemberData(
                    self.data.details, loc_missing=NoLocReason.NOT_FOUND
                )
            elif old_md := self._member_data.get(cid):
                # NOT_MODIFIED or NO_DATA
                member_data[cid] = old_md
        if not member_data:
            return self.data

        # Save the data in case NotModified or server error on next cycle.
        self._member_data = member_data

        # Now take "best" data for Member.
        data = sorted(member_data.values())[-1]
        if len(self._coordinator.data.circles) > 1:
            # Each Circle has its own Places. Collect all the Places where the
            # Member might be, while keeping the Circle they came from. Then
            # update the chosen MemberData with the Place or Places where the
            # Member is, with each having a suffix of the name of its Circle.
            places = {
                cid: cast(str, md.loc.details.place)
                for cid, md in member_data.items()
                if md.loc and md.loc.details.place
            }
            if places:
                place: str | list[str] = [
                    f"{c_place} ({self._coordinator.data.circles[cid].name})"
                    for cid, c_place in places.items()
                ]
                if len(place) == 1:
                    place = place[0]
                data = deepcopy(data)
                assert data.loc
                data.loc.details.place = place

        return data


@dataclass
class L360Coordinators:
    """Life360 data update coordinators."""

    coordinator: CirclesMembersDataUpdateCoordinator
    mem_coordinator: dict[MemberID, MemberDataUpdateCoordinator]


type L360ConfigEntry = ConfigEntry[L360Coordinators]
