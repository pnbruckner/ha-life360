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

from homeassistant.components.device_tracker import DOMAIN as DT_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
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
    SIGNAL_ACCT_STATUS,
    UPDATE_INTERVAL,
)
from .helpers import (
    AccountID,
    CircleData,
    CircleID,
    ConfigOptions,
    Life360Store,
    MemberData,
    MemberDetails,
    MemberID,
    Members,
)

_LOGGER = logging.getLogger(__name__)

_R = TypeVar("_R")
_Ts = TypeVarTuple("_Ts")


@dataclass
class CircleMemberData:
    """Circle & Member data."""

    # These come from the server, and are stored.
    circles: dict[CircleID, CircleData] = field(default_factory=dict)
    mem_details: dict[MemberID, MemberDetails] = field(default_factory=dict)
    # This is derived from circles above for run-time convenience.
    mem_circles: dict[MemberID, set[CircleID]] = field(default_factory=dict)


@dataclass
class AccountData:
    """Data for a Life360 account."""

    session: ClientSession
    api: helpers.Life360
    failed: asyncio.Event
    failed_task: asyncio.Task
    online: bool = True


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
        self._cm_data = self._load_cm_data()
        # TODO: Remove once implementation is stable.
        self._validate_cm_data()
        self.data = {
            mid: MemberData(md) for mid, md in self._cm_data.mem_details.items()
        }
        # TODO: Create a sensor that lists all the Circles and which Members are in each
        #       and eventually, the names of the Places in each.
        self._options = ConfigOptions.from_dict(self.config_entry.options)
        self._acct_data: dict[AccountID, AccountData] = {}
        self._create_acct_data(self._options.accounts)
        self._member_circle_data: dict[MemberID, dict[CircleID, MemberData]] = {}

        self.config_entry.async_on_unload(
            self.config_entry.add_update_listener(self._config_entry_updated)
        )
        self.config_entry.async_on_unload(self._update_cm_data_stop)

    def acct_online(self, aid: AccountID) -> bool:
        """Return if account is online."""
        return self._acct_data[aid].online

    # TODO: Remove this once implementation is stable.
    def _validate_cm_data(self) -> None:
        """Validate CircleMemberData is consistent."""
        mem_circles: dict[MemberID, set[CircleID]] = {}
        for cid, circle_data in self._cm_data.circles.items():
            assert circle_data.name
            # Every Circle must be accessible from at least one account.
            assert circle_data.aids
            for mid in circle_data.mids:
                mem_circles.setdefault(mid, set()).add(cid)
        assert self._cm_data.mem_circles == mem_circles
        assert set(mem_circles) == set(self._cm_data.mem_details)
        for md in self._cm_data.mem_details.values():
            assert md.name

    async def _async_update_data(self) -> Members:
        """Fetch the latest data from the source with lock."""
        assert self._update_data_task is None
        self._update_data_task = cast(asyncio.Task, asyncio.current_task())
        try:
            async with self._update_data_lock:
                return await self._do_update_data()
        except asyncio.CancelledError:
            self._update_data_task.uncancel()
            return self.data
        finally:
            self._update_data_task = None

    async def _do_update_data(self) -> Members:
        """Fetch the latest data from the source."""
        # TODO: How to handle errors, especially per aid/api???
        result = Members()

        if self._first_refresh:
            await self._update_cm_data_start(retry_first=False)
            self.data = {
                mid: MemberData(md) for mid, md in self._cm_data.mem_details.items()
            }
            self._first_refresh = False

        cm_data = self._cm_data

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
                elif old_mcd and (old_md := old_mcd.get(cid)):
                    member_circle_data[cid] = old_md
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

            else:
                result[mid] = self.data[mid]

        return result

    def _load_cm_data(self) -> CircleMemberData:
        """Load Circles & Members from storage."""
        if not self._store.loaded_ok:
            _LOGGER.warning(
                "Could not load Circles & Members from storage"
                "; will wait for data from server"
            )
            return CircleMemberData()

        circles = self._store.circles
        mem_details = self._store.mem_details

        # TODO: Remove before beta.
        # Earlier versions did not store mem_details, so the names might be None. If so,
        # try to fill them in from the entity registry. Worst case, use Member ID.
        ent_reg = er.async_get(self.hass)
        for mid, md in mem_details.items():
            if cast(str | None, md.name) is not None:
                continue
            if (
                (entity_id := ent_reg.async_get_entity_id(DT_DOMAIN, DOMAIN, mid))
                and (reg_entry := ent_reg.async_get(entity_id))
                and (name := (reg_entry.name or reg_entry.original_name))
            ):
                md.name = name
            else:
                md.name = mid

        mem_circles: dict[MemberID, set[CircleID]] = {}
        for cid, circle_data in circles.items():
            for mid in circle_data.mids:
                mem_circles.setdefault(mid, set()).add(cid)

        return CircleMemberData(circles, mem_details, mem_circles)

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

        # Get Members in each Circle, recording their name & entity_picture, and keeping
        # track of which Circles each Member is in, since a Member can be in more than
        # one Circle.
        mem_details: dict[MemberID, MemberDetails] = {}
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
                        mid = MemberID(raw_member["id"])
                        circle_data.mids.add(mid)
                        if mid not in mem_details:
                            mem_details[mid] = MemberDetails.from_server(raw_member)
                        mem_circles.setdefault(mid, set()).add(cid)
                    break

        # If there were any errors while getting Circles for each account, then retry
        # must have been False. Since we haven't yet received Circle data for all
        # enabled accounts, use any old information that is available to fill in the
        # gaps for now. E.g., we don't want to remove any Member entity until we're
        # absolutely sure they are no longer in any Circle visible from all enabled
        # accounts.
        if circle_errors:
            for cid, old_circle_data in self._cm_data.circles.items():
                if cid in circles:
                    circles[cid].aids |= old_circle_data.aids
                else:
                    circles[cid] = old_circle_data
                    for mid in old_circle_data.mids:
                        mem_circles.setdefault(mid, set()).add(cid)
            for mid, old_md in self._cm_data.mem_details.items():
                if mid not in mem_details:
                    mem_details[mid] = old_md

        self._cm_data = CircleMemberData(circles, mem_details, mem_circles)
        # TODO: Remove once implementation is stable.
        self._validate_cm_data()

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

        start = dt_util.now()
        delay: int | None = None
        delay_reason = ""
        warned = False

        failed_task = self._acct_data[aid].failed_task
        try:
            while True:
                if delay is not None:
                    if (
                        not warned
                        and (dt_util.now() - start).total_seconds() + delay > 60 * 60
                    ):
                        _LOGGER.warning(
                            "Getting response from Life360 for %s "
                            "is taking longer than expected",
                            aid,
                        )
                        warned = True
                    _LOGGER.debug(
                        "%s: %s %s: will retry in %i s", aid, delay_reason, msg, delay
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
                    request_task.cancel()
                    with suppress(asyncio.CancelledError, Life360Error):
                        await request_task
                    return RequestError.NO_DATA

                try:
                    # if aid == "federicktest95@gmail.com":
                    #     self._requests += 1
                    #     if self._requests == 1:
                    #         request_task.cancel()
                    #         with suppress(BaseException):
                    #             await request_task
                    #         raise LoginError("TEST TEST TEST")
                    result = await request_task
                    self._set_acct_online(aid, True)
                    return result
                except NotModified:
                    self._set_acct_online(aid, True)
                    return RequestError.NOT_MODIFIED
                except LoginError as exc:
                    if lrle_resp is LoginRateLimitErrorResp.RETRY:
                        self._set_acct_online(aid, True)
                        delay = 15 * 60
                        delay_reason = "login error"
                        continue

                    treat_as_error = lrle_resp is not LoginRateLimitErrorResp.SILENT
                    self._set_acct_online(aid, not treat_as_error)
                    level = logging.ERROR if treat_as_error else logging.DEBUG
                    _LOGGER.log(level, "%s: login error %s: %s", aid, msg, exc)
                    if treat_as_error:
                        self._handle_login_error(aid)
                    return RequestError.NO_DATA
                except Life360Error as exc:
                    rate_limited = isinstance(exc, RateLimited)
                    if lrle_resp is LoginRateLimitErrorResp.RETRY and rate_limited:
                        self._set_acct_online(aid, True)
                        delay = ceil(cast(RateLimited, exc).retry_after or 0) + 10
                        delay_reason = "rate limited"
                        continue

                    # TODO: Keep track of errors per aid so we don't flood log???
                    #       Maybe like DataUpdateCoordinator does it?
                    treat_as_error = not (
                        rate_limited and lrle_resp is LoginRateLimitErrorResp.SILENT
                    )
                    self._set_acct_online(aid, not treat_as_error)
                    level = logging.ERROR if treat_as_error else logging.DEBUG
                    _LOGGER.log(level, "%s: %s: %s", aid, msg, exc)
                    return RequestError.NO_DATA
        finally:
            if warned:
                _LOGGER.warning("Done trying to get response from Life360 for %s", aid)

    def _set_acct_online(self, aid: AccountID, online: bool) -> None:
        """Set account online status and signal clients if it has changed."""
        if online == (acct := self._acct_data[aid]).online:
            return
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

        # Stop everything. Note that if _async_update_data gets cancelled, it will still
        # be scheduled to run again, so that does not need to be done here.
        await self._update_cm_data_stop()
        if update_data_task := self._update_data_task:
            update_data_task.cancel()

        # Prevent _do_update_data from running while _acct_data & _cm_data are being
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
            for cid, circle_data in self._cm_data.circles.items():
                circle_data.aids -= del_acct_ids
                if not circle_data.aids:
                    no_aids.append(cid)
            no_circles: list[MemberID] = []
            for cid in no_aids:
                del self._cm_data.circles[cid]
                for mid, mem_circles in self._cm_data.mem_circles.items():
                    mem_circles.discard(cid)
                    if not mem_circles:
                        no_circles.append(mid)
            for mid in no_circles:
                del self._cm_data.mem_details[mid]
                del self._cm_data.mem_circles[mid]

            # TODO: Remove once implementation is stable.
            self._validate_cm_data()

            await self._update_cm_data_start()

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
