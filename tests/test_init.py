"""Test Life360 __init__.py module."""
from __future__ import annotations

from collections.abc import Iterable, Mapping, MutableMapping
from dataclasses import dataclass
from functools import partial
from itertools import repeat
import re
from typing import Any, Self, cast

from custom_components.life360.config_flow import Life360ConfigFlow
from custom_components.life360.const import (
    ATTR_REASON,
    ATTRIBUTION,
    DOMAIN,
    UPDATE_INTERVAL,
)
from custom_components.life360.helpers import AccountID, ConfigOptions, MemberID
from life360 import LoginError
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    assert_setup_component,
    async_fire_time_changed,
)

from homeassistant.components.binary_sensor import DOMAIN as BS_DOMAIN
from homeassistant.components.device_tracker import (
    ATTR_SOURCE_TYPE,
    DOMAIN as DT_DOMAIN,
    SourceType,
)
from homeassistant.const import (
    ATTR_ATTRIBUTION,
    ATTR_ENTITY_PICTURE,
    ATTR_FRIENDLY_NAME,
    STATE_OFF,
    STATE_ON,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import slugify

from .common import DtNowMock, assert_log_messages

StoreData = Mapping[str, Mapping[str, Mapping[str, Any]]]
MutableStoreData = MutableMapping[str, MutableMapping[str, MutableMapping[str, Any]]]
StorageInfo = Mapping[str, int | StoreData]
Storage = Mapping[str, StorageInfo]
MutableStorage = MutableMapping[str, StorageInfo]


@dataclass
class MemberInfo:
    """Member info."""

    mid: str
    name: str
    entity_picture: str | None
    sharing: bool
    reason: str | None
    loc: dict[str, Any] | None

    @classmethod
    def from_data(cls, member: Mapping[str, Any]) -> Self:
        """Initialize from Member data."""
        sharing = bool(int(member["features"]["shareLocation"]))
        loc = member["location"]
        reason: str | None = None
        if not sharing:
            reason = "Member is not sharing location"
        elif not loc:
            if title := member["issues"]["title"]:
                reason = title
                if dialog := member["issues"]["dialog"]:
                    reason = f"{reason}: {dialog}"
            else:
                reason = (
                    "The user may have lost connection to Life360. "
                    "See https://www.life360.com/support/"
                )
        return cls(
            member["id"],
            " ".join([member["firstName"], member["lastName"]]),
            member["avatar"],
            sharing,
            reason,
            loc,
        )


def cfg_options(
    accts: int,
    driving: bool = False,
    driving_speed: float | None = None,
    max_gps_accuracy: int | None = None,
    verbosity: int = 0,
) -> dict[str, Any]:
    """Create config options."""
    return {
        "accounts": {
            f"aid{i}": {"authorization": f"auth{i}", "password": None, "enabled": True}
            for i in range(1, accts + 1)
        },
        "driving": driving,
        "driving_speed": driving_speed,
        "max_gps_accuracy": max_gps_accuracy,
        "verbosity": verbosity,
    }


def assert_stored_data(
    hass_storage: Storage,
    circles: Mapping[str, Mapping[str, Any]],
    members: Iterable[Mapping[str, Any]],
) -> None:
    """Check that stored data is as expected."""
    store = hass_storage.get(DOMAIN)
    assert store
    assert (stored_data := cast(StoreData | None, store.get("data")))
    stored_circles = stored_data["circles"]
    for cid, circle in circles.items():
        assert cid in stored_circles
        assert set(stored_circles[cid]["aids"]) == set(circle["aids"])
        assert set(stored_circles[cid]["mids"]) == set(circle["mids"])
    stored_mem_details = stored_data["mem_details"]
    for member in members:
        mem_info = MemberInfo.from_data(member)
        assert mem_info.mid in stored_mem_details
        assert stored_mem_details[mem_info.mid] == {
            "name": mem_info.name,
            "entity_picture": mem_info.entity_picture,
        }


empty_store: StoreData = {"circles": {}, "mem_details": {}}


@pytest.mark.parametrize("store_data", [None, empty_store])
@pytest.mark.parametrize("options", [cfg_options(1), cfg_options(2)])
async def test_no_circles_members(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    hass_storage: MutableStorage,
    caplog: pytest.LogCaptureFixture,
    options: dict[str, Any],
    store_data: StoreData | None,
):
    """Test w/ no Circles or Members, w/ or w/o store data."""
    if store_data is not None:
        hass_storage[DOMAIN] = {"version": 1, "data": store_data}
    entry = MockConfigEntry(
        domain=DOMAIN, version=Life360ConfigFlow.VERSION, options=options
    )
    entry.add_to_hass(hass)

    with assert_setup_component(0, DOMAIN):
        assert await async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done()

    # Check WARNING messages.
    expected = 1 if store_data is None else 0
    message1 = (
        "Could not load Circles & Members from storage; will wait for data from server"
    )
    messages = (
        (expected, "WARNING", message1),
        (expected, "WARNING", "Circles & Members list retrieval complete"),
    )
    assert_log_messages(caplog, messages)

    # Check that only binary online sensors were created, one per account.
    accts = cast(dict[str, Any], options["accounts"])
    entity_ids = [
        entity_id
        for entity_id, entity in entity_registry.entities.items()
        if entity.platform == DOMAIN
    ]
    assert len(entity_ids) == len(accts)
    for aid in accts:
        entity_id = f"binary_sensor.life360_online_{aid}"
        assert entity_id in entity_ids
        # Check that binary online sensor is on.
        state = hass.states.get(entity_id)
        assert state
        assert state.state == STATE_ON

    # Check that Circles & Members have been written to storage.
    assert_stored_data(hass_storage, {}, [])


cir1 = {"id": "cid1", "name": "Circle1"}
mem1 = {
    "id": "mid1",
    "firstName": "First1",
    "lastName": "Last1",
    "avatar": None,
    "features": {"shareLocation": 0},
    "location": None,
    "issues": {"title": "", "dialog": ""},
}
mem2 = {
    "id": "mid2",
    "firstName": "First2",
    "lastName": "Last2",
    "avatar": "EP2",
    "features": {"shareLocation": 1},
    "location": None,
    "issues": {"title": "ISSUE TITLE", "dialog": "ISSUE DIALOG"},
}


def get_circle_member(
    member_data: Mapping[str, Mapping[str, dict[str, Any]]],
    cid: str,
    mid: str,
    *,
    raise_not_modified: bool = False,
) -> dict[str, Any]:
    """Get details for Member as seen from given Circle."""
    return member_data[cid][mid]


# fmt: off
@pytest.mark.parametrize(
    ("MockLife360", "circles", "members"),
    [
        # 1 Account, 1 Circle, no Members
        (
            {"aid1": {"get_circles": repeat([cir1])}},
            {cir1["id"]: {"name": cir1["name"], "aids": ["aid1"], "mids": []}},
            [],
        ),
        # 1 Account, 1 Circle, 1 Member not sharing location
        (
            {
                "aid1": {
                    "get_circles": repeat([cir1]),
                    "get_circle_members": repeat([mem1]),
                    "get_circle_member": repeat(mem1),
                },
            },
            {
                cir1["id"]: {
                    "name": cir1["name"], "aids": ["aid1"], "mids": [mem1["id"]]
                },
            },
            [mem1],
        ),
        # 1 Account, 1 Circle, 2 Members, 1 sharing location (but missing), 1 not
        (
            {
                "aid1": {
                    "get_circles": repeat([cir1]),
                    "get_circle_members": repeat([mem1, mem2]),
                    "get_circle_member": partial(
                        get_circle_member,
                        {cir1["id"]: {mem1["id"]: mem1, mem2["id"]: mem2}},
                    ),
                },
            },
            {
                cir1["id"]: {
                    "name": cir1["name"],
                    "aids": ["aid1"],
                    "mids": [mem1["id"], mem2["id"]],
                },
            },
            [mem1, mem2],
        ),
    ],
    indirect=["MockLife360"],
)
# fmt: on
async def test_circles_members_no_loc(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    hass_storage: MutableStorage,
    caplog: pytest.LogCaptureFixture,
    circles: Mapping[str, Mapping[str, Any]],
    members: Iterable[Mapping[str, Any]],
):
    """Test w/ Circles & Members w/ no location data."""
    # Use higher verbosity so that API name is AccountID.
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=Life360ConfigFlow.VERSION,
        options=cfg_options(1, verbosity=3),
    )
    entry.add_to_hass(hass)

    with assert_setup_component(0, DOMAIN):
        assert await async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done()

    # Check that a device_tracker entity has been created for each Member.
    for mem_info in [MemberInfo.from_data(member) for member in members]:
        expected_entity_name = f"Life360 {mem_info.name}"
        expected_entity_id = f"{DT_DOMAIN}.{slugify(expected_entity_name)}"

        entity_id = entity_registry.async_get_entity_id(DT_DOMAIN, DOMAIN, mem_info.mid)
        assert entity_id
        assert entity_id == expected_entity_id

        # Check entity's state.
        state = hass.states.get(entity_id)
        assert state
        assert state.state == STATE_UNKNOWN
        # Check entity's attributes.
        expected_attrs = {
            ATTR_ATTRIBUTION: ATTRIBUTION,
            ATTR_FRIENDLY_NAME: expected_entity_name,
            ATTR_REASON: mem_info.reason,
            ATTR_SOURCE_TYPE: SourceType.GPS,
        }
        if mem_info.entity_picture:
            expected_attrs[ATTR_ENTITY_PICTURE] = mem_info.entity_picture
        assert state.attributes == expected_attrs
        # Check location is missing.
        assert not mem_info.loc

        # Check WARNING has been issued for Member.
        pat = re.compile(
            rf"Location data for {expected_entity_name} \({expected_entity_id}\)"
            rf" is missing: {mem_info.reason}"
        )
        assert_log_messages(caplog, ((1, "WARNING", pat),))

    # Check that Circles & Members have been written to storage.
    assert_stored_data(hass_storage, circles, members)


@pytest.mark.parametrize(
    "MockLife360",
    [
        {
            "aid1": {
                "get_circles": [LoginError("TEST: Forbidden"), [cir1]],
                "get_circle_members": repeat([mem1, mem2]),
                "get_circle_member": partial(
                    get_circle_member,
                    {cir1["id"]: {mem1["id"]: mem1, mem2["id"]: mem2}},
                ),
            },
        },
    ],
    indirect=["MockLife360"],
)
async def test_circles_members_delayed(
    hass: HomeAssistant,
    hass_storage: MutableStorage,
    caplog: pytest.LogCaptureFixture,
):
    """Test w/ Circles & Members w/ first request fails."""
    # Start with Circle & one Member in storage.
    mem1_info = MemberInfo.from_data(mem1)
    circles = {
        cir1["id"]: {"name": cir1["name"], "aids": ["aid1"], "mids": [mem1_info.mid]},
    }
    store_data: StoreData = {
        "circles": circles,
        "mem_details": {
            mem1_info.mid: {
                "name": mem1_info.name,
                "entity_picture": mem1_info.entity_picture,
            },
        },
    }
    hass_storage[DOMAIN] = {"version": 1, "data": store_data}

    # Use higher verbosity so that API name is AccountID.
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=Life360ConfigFlow.VERSION,
        options=cfg_options(1, verbosity=3),
    )
    entry.add_to_hass(hass)

    with assert_setup_component(0, DOMAIN):
        assert await async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done()

    # First retrieval of Circles failed, but second attempt succeeded.
    # There should now be two Members.

    # Check for expected WARNING messages.
    messages = (
        (
            1,
            "WARNING",
            "Could not retrieve full Circles & Members list from server; will retry",
        ),
        (1, "WARNING", "Circles & Members list retrieval complete"),
    )
    assert_log_messages(caplog, messages)

    # Check that Circles & Members have been written to storage.
    cast(list[str], circles[cir1["id"]]["mids"]).append(cast(str, mem2["id"]))
    assert_stored_data(hass_storage, circles, [mem1, mem2])


@pytest.mark.parametrize(
    "MockLife360",
    [
        {
            "aid1": {
                "get_circles": repeat([cir1]),
                "get_circle_members": iter([[mem1], [mem1, mem2]]),
                "get_circle_member": partial(
                    get_circle_member,
                    {cir1["id"]: {mem1["id"]: mem1, mem2["id"]: mem2}},
                ),
            },
        },
    ],
    indirect=["MockLife360"],
)
async def test_reload_new_member(hass: HomeAssistant, hass_storage: MutableStorage):
    """Test reload with new Member after reload."""
    # Use higher verbosity so that API name is AccountID.
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=Life360ConfigFlow.VERSION,
        options=cfg_options(1, verbosity=3),
    )
    entry.add_to_hass(hass)

    with assert_setup_component(0, DOMAIN):
        assert await async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done()

    # Initially there is only one Member.
    circles = {
        cir1["id"]: {"name": cir1["name"], "aids": ["aid1"], "mids": [mem1["id"]]},
    }
    assert_stored_data(hass_storage, circles, [mem1])

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    # Now there are two Members.
    cast(list[str], circles[cir1["id"]]["mids"]).append(cast(str, mem2["id"]))
    assert_stored_data(hass_storage, circles, [mem1, mem2])


LOGIN_ERROR_MESSAGE = "TEST: Login error"


@pytest.mark.parametrize(
    "MockLife360",
    [
        {
            "aid1": {
                "get_circles": repeat([cir1]),
                "get_circle_members": repeat([mem1]),
                "get_circle_member": iter(
                    [mem1, LoginError(LOGIN_ERROR_MESSAGE), mem1]
                ),
            },
        },
    ],
    indirect=["MockLife360"],
)
async def test_login_error(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    issue_registry: ir.IssueRegistry,
    caplog: pytest.LogCaptureFixture,
    dt_now: DtNowMock,
):
    """Test login error while getting Member data."""
    dt_now_real, dt_now_mock = dt_now

    # Use higher verbosity so that API name is AccountID.
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=Life360ConfigFlow.VERSION,
        options=cfg_options(1, verbosity=3),
    )
    entry.add_to_hass(hass)

    with assert_setup_component(0, DOMAIN):
        assert await async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done()

    aid = AccountID("aid1")
    mid = MemberID(cast(str, mem1["id"]))

    # Check that account binary online sensor exists and is on.
    bs_entity_id = entity_registry.async_get_entity_id(BS_DOMAIN, DOMAIN, aid)
    assert bs_entity_id
    state = hass.states.get(bs_entity_id)
    assert state
    assert state.state == STATE_ON

    # Check that device_tracker exists and is unknown.
    dt_entity_id = entity_registry.async_get_entity_id(DT_DOMAIN, DOMAIN, mid)
    assert dt_entity_id
    state = hass.states.get(dt_entity_id)
    assert state
    assert state.state == STATE_UNKNOWN

    # Advance "time" so Member coordinator will update.
    now = dt_now_real() + UPDATE_INTERVAL
    dt_now_mock.return_value = now
    async_fire_time_changed(hass, now)
    try:
        await hass.async_block_till_done(wait_background_tasks=True)
    except TypeError:
        await hass.async_block_till_done()

    # Check that account was disabled.
    options = ConfigOptions.from_dict(entry.options)
    assert not options.accounts[aid].enabled

    # Check that an ERROR message was issued.
    pat = re.compile(rf"{aid}: while getting data for .*: {LOGIN_ERROR_MESSAGE}.*")
    assert_log_messages(caplog, ((1, "ERROR", pat),))

    # Check that repair issue was created for account.
    issue = issue_registry.async_get_issue(DOMAIN, aid)
    assert issue
    assert not issue.is_fixable and issue.is_persistent
    assert issue.active and issue.severity == ir.IssueSeverity.ERROR

    # Check that account binary online sensor exists and is off.
    state = hass.states.get(bs_entity_id)
    assert state
    assert state.state == STATE_OFF

    # Check that device_tracker exists and is unknown.
    state = hass.states.get(dt_entity_id)
    assert state
    assert state.state == STATE_UNKNOWN

    # Simulate "fixing" error by re-enabling account.
    # NOTE: This will not remove repair issue. That is done by config flow and will be
    #       checked in config flow tests.
    options.accounts[aid].enabled = True
    hass.config_entries.async_update_entry(entry, options=options.as_dict())
    await hass.async_block_till_done()

    # Check that account binary online sensor exists and is on.
    state = hass.states.get(bs_entity_id)
    assert state
    assert state.state == STATE_ON


# async def test_remove_entry(
#     hass: HomeAssistant,
#     hass_storage: dict[str, Any],
#     bs_setup_entry_mock: AsyncMock,
#     dt_setup_entry_mock: AsyncMock,
#     coordinator_mock: MagicMock,
# ) -> None:
#     """Test config entry removal."""
#     hass_storage[DOMAIN] = {"version": 1, "data": empty_store}

#     v2_entry = MockConfigEntry(domain=DOMAIN, version=2)
#     v2_entry.add_to_hass(hass)

#     crd = coordinator_mock.return_value
#     crd.data = CirclesMembersData()

#     with assert_setup_component(0, DOMAIN):
#         assert await async_setup_component(hass, DOMAIN, {})
#         await hass.async_block_till_done()

#     assert len(hass.config_entries.async_entries(DOMAIN)) == 1
#     assert DOMAIN in hass_storage

#     assert await hass.config_entries.async_remove(v2_entry.entry_id)
#     await hass.async_block_till_done()

#     assert not hass.config_entries.async_entries(DOMAIN)
#     assert DOMAIN not in hass_storage
