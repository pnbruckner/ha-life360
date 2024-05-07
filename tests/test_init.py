"""Test Life360 __init__.py module."""
from __future__ import annotations

from collections.abc import Generator, Iterable
from itertools import chain, repeat
from math import ceil
import re
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.life360.const import DOMAIN
from custom_components.life360.helpers import (
    CircleData,
    CircleID,
    CirclesMembersData,
    Life360Store,
    MemberDetails,
    MemberID,
)
from life360 import LoginError
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    assert_setup_component,
)

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfSpeed
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import uuid as uuid_util
from homeassistant.util.unit_conversion import SpeedConverter

from .conftest import Life360API_Data

# ========== Fixtures ==================================================================


@pytest.fixture
def setup_entry_mock(hass: HomeAssistant) -> Generator[AsyncMock, None, None]:
    """Mock async_setup_entry."""
    with patch("custom_components.life360.async_setup_entry", autospec=True) as mock:
        # Teardown, in newer HA versions, will unload config entries. Add DOMAIN to
        # hass.data so real async_unload_entry won't cause an exception.
        hass.data[DOMAIN] = None
        mock.return_value = True
        yield mock


@pytest.fixture
def unload_entry_mock() -> Generator[AsyncMock, None, None]:
    """Mock async_unload_entry."""
    with patch("custom_components.life360.async_unload_entry", autospec=True) as mock:
        mock.return_value = True
        yield mock


@pytest.fixture
def remove_entry_mock() -> Generator[AsyncMock, None, None]:
    """Mock async_remove_entry."""
    with patch("custom_components.life360.async_remove_entry", autospec=True) as mock:
        mock.return_value = True
        yield mock


@pytest.fixture
def bs_setup_entry_mock() -> Generator[AsyncMock, None, None]:
    """Mock binary_sensor async_setup_entry."""
    with patch(
        "custom_components.life360.binary_sensor.async_setup_entry", autospec=True
    ) as mock:
        yield mock


@pytest.fixture
def dt_setup_entry_mock() -> Generator[AsyncMock, None, None]:
    """Mock device_tracker async_setup_entry."""
    with patch(
        "custom_components.life360.device_tracker.async_setup_entry", autospec=True
    ) as mock:
        yield mock


@pytest.fixture
def coordinator_mock() -> Generator[MagicMock, None, None]:
    """Mock CirclesMembersDataUpdateCoordinator."""
    with patch(
        "custom_components.life360.CirclesMembersDataUpdateCoordinator", autospec=True
    ) as mock:
        yield mock


@pytest.fixture
def mem_coordinator_mock() -> Generator[MagicMock, None, None]:
    """Mock MemberDataUpdateCoordinator."""
    with patch(
        "custom_components.life360.MemberDataUpdateCoordinator", autospec=True
    ) as mock:
        yield mock


# ========== async_setup Tests: Migration ==============================================


circles = [{"id": "cid1", "name": "Circle 1"}, {"id": "cid2", "name": "Circle 2"}]
member1 = {
    "id": "mid1",
    "firstName": "First1",
    "lastName": "Last1",
    "avatar": None,
    "features": {"shareLocation": 0},
    "loc": None,
}
member2 = {
    "id": "mid2",
    "firstName": "First2",
    "lastName": "Last2",
    "avatar": None,
    "features": {"shareLocation": 0},
    "loc": None,
}
circle_members = {"cid1": [member1], "cid2": [member2]}
members = {"cid1": {"mid1": member1}, "cid2": {"mid2": member2}}


def get_circle_members(
    cid: str, *, raise_not_modified: bool = False
) -> list[dict[str, Any]]:
    """Get details for Members in given Circle."""
    return circle_members[cid]


def get_circle_member(
    cid: str, mid: str, *, raise_not_modified: bool = False
) -> dict[str, Any]:
    """Get details for Member as seen from given Circle."""
    return members[cid][mid]


def api_data() -> Life360API_Data:
    """Generate Life360 API data."""
    return {
        "Account 1": {
            "get_circles": chain(repeat(LoginError("TEST"), 1), repeat(circles)),
            "get_circle_members": get_circle_members,
            "get_circle_member": get_circle_member,
        },
    }


# fmt: off
@pytest.mark.parametrize(
    ("MockLife360", "metric", "option_set"),
    [
        (
            api_data(),
            False,
            (
                # driving_speed, max_gps_accuracy, driving
                (None, None, False),
            ),
        ),
        (
            api_data(),
            False,
            (
                (10.0, 50.0, True),
            ),
        ),
        (
            api_data(),
            True,
            (
                (10.0, 50.0, True),
            ),
        ),
        (
            api_data(),
            False,
            (
                (None, None, False),
                (10.0, 50.0, False),
            ),
        ),
        (
            api_data(),
            False,
            (
                (None, None, False),
                (10.0, 50.0, True),
                (15.0, 100.0, False),
            ),
        ),
    ],
    indirect=["MockLife360"],
)
# fmt: on
async def test_migration(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    caplog: pytest.LogCaptureFixture,
    setup_entry_mock: AsyncMock,
    unload_entry_mock: AsyncMock,
    remove_entry_mock: AsyncMock,
    metric: bool,
    option_set: Iterable[tuple[float, float, bool]],
) -> None:
    """Test config entry migration."""

    if not metric:
        await hass.config.async_update(unit_system="us_customary")
        await hass.async_block_till_done()

    # Mock v1 config entries w/ associated entities.

    un_f = lambda i: f"user_{i}@email_{i}.com"
    pw_f = lambda i: f"password_{i}"
    au_f = lambda i: f"authorization_{i}"

    cfg_accts: dict[str, dict[str, Any]] = {}
    comb_ds: float | None = None
    comb_ma: int | None = None
    comb_dr: bool = False
    v1_entries: list[ConfigEntry] = []
    v1_entry_ids: list[str] = []
    entity_ids: list[str] = []
    for i, o in enumerate(option_set):
        un = un_f(i)
        pw = pw_f(i)
        au = au_f(i)
        ds, ma, dr = o

        cfg_accts[un] = {"password": pw, "authorization": au, "enabled": True}
        if ds is not None:
            if comb_ds is None:
                comb_ds = ds
            else:
                comb_ds = min(comb_ds, ds)
        if ma is not None:
            ima = ceil(ma)
            if comb_ma is None:
                comb_ma = ima
            else:
                comb_ma = max(comb_ma, ima)
        comb_dr |= dr

        v1_entry = MockConfigEntry(
            domain=DOMAIN,
            data={"username": un, "password": pw, "authorization": au},
            options={"driving_speed": ds, "max_gps_accuracy": ma, "driving": dr},
        )
        v1_entry.add_to_hass(hass)
        v1_entries.append(v1_entry)
        v1_entry_ids.append(v1_entry.entry_id)

        name = f"life360 online ({un})"
        entity_ids.append(
            entity_registry.async_get_or_create(
                "binary_sensor",
                DOMAIN,
                un,
                suggested_object_id=name,
                config_entry=v1_entry,
                original_device_class=BinarySensorDeviceClass.CONNECTIVITY,
                original_name=name,
            ).entity_id
        )

        name = f"User {i}"
        entity_ids.append(
            entity_registry.async_get_or_create(
                "device_tracker",
                DOMAIN,
                uuid_util.random_uuid_hex(),
                suggested_object_id=name,
                config_entry=v1_entry,
                entity_category=EntityCategory.DIAGNOSTIC,
                original_name=name,
            ).entity_id
        )

    with assert_setup_component(0, DOMAIN):
        assert await async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done()

    # Check that v2 config entry was created and v1 config entries are gone.
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    v2_entry = entries[0]
    assert v2_entry.version == 2
    assert v2_entry.entry_id not in v1_entry_ids
    # Check that v1 config entries got removed.
    for entry in v1_entries:
        remove_entry_mock.assert_any_call(hass, entry)
    # Check that v2 config entry got setup.
    setup_entry_mock.assert_called_once_with(hass, v2_entry)

    # Check v2 config entry options.
    if comb_ds is not None and metric:
        comb_ds = SpeedConverter.convert(
            comb_ds,
            UnitOfSpeed.KILOMETERS_PER_HOUR,
            UnitOfSpeed.MILES_PER_HOUR,
        )
    assert v2_entry.options == {
        "accounts": cfg_accts,
        "driving_speed": comb_ds,
        "max_gps_accuracy": comb_ma,
        "driving": comb_dr,
        "verbosity": 0,
    }

    # Check that entities have been reassigned to v2 config entry.
    for entity_id in entity_ids:
        entity = entity_registry.async_get(entity_id)
        assert entity
        assert entity.config_entry_id == v2_entry.entry_id

    # Check that a warning message was created noting the migration.
    assert any(
        rec.levelname == "WARNING"
        and "Migrating Life360 integration entries from version 1 to 2" in rec.message
        for rec in caplog.get_records("call")
    )


@pytest.mark.parametrize("entity_migrated", [False, True])
async def test_aborted_migration(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    caplog: pytest.LogCaptureFixture,
    setup_entry_mock: AsyncMock,
    unload_entry_mock: AsyncMock,
    entity_migrated: bool,
) -> None:
    """Test aborted & restarted config entry migration."""
    # Set up partial migration scenario with v2 entry created, but v1 entry still
    # still present and entity possibly still associated with v1 entry.
    v1_entry = MockConfigEntry(domain=DOMAIN)
    v1_entry.add_to_hass(hass)
    v2_entry = MockConfigEntry(domain=DOMAIN, version=2)
    v2_entry.add_to_hass(hass)
    entity_id = entity_registry.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        "user@email.com",
        config_entry=v2_entry if entity_migrated else v1_entry,
        original_device_class=BinarySensorDeviceClass.CONNECTIVITY,
    ).entity_id

    with assert_setup_component(0, DOMAIN):
        assert await async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done()

    # Check that v1 config entry is gone.
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    assert entries[0] is v2_entry
    # Check that v2 config entry got setup.
    setup_entry_mock.assert_called_once_with(hass, v2_entry)

    # Check that entity is associated with v2 config entry.
    entity = entity_registry.async_get(entity_id)
    assert entity
    assert entity.config_entry_id == v2_entry.entry_id

    # Check that a warning message was NOT created noting the migration.
    assert not any(
        rec.levelname == "WARNING"
        and "Migrating Life360 integration entries from version 1 to 2" in rec.message
        for rec in caplog.get_records("call")
    )


async def test_uknown_config_version(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    setup_entry_mock: AsyncMock,
    unload_entry_mock: AsyncMock,
) -> None:
    """Test with unknown config entry version (i.e., downgrading)."""

    entry = MockConfigEntry(domain=DOMAIN, version=3)
    entry.add_to_hass(hass)

    with assert_setup_component(0, DOMAIN):
        assert not await async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done()

    for levelname, pat in (
        ("ERROR", r"Unsupported configuration entry found: [^,]+, version: 3(.1)?"),
        (
            "ERROR",
            (
                r"Setup failed for custom integration '?life360'?: "
                r"Integration failed to initialize\."
            ),
        ),
    ):
        assert any(
            rec.levelname == levelname and re.fullmatch(pat, rec.message)
            for rec in caplog.get_records("call")
        )


# ========== config entry Tests ========================================================


StoreData = dict[str, dict[str, dict[str, Any]]]
empty_store: StoreData = {"circles": {}, "mem_details": {}}


# fmt: off
@pytest.mark.parametrize(
    "store_data",
    [
        None,
        empty_store,
        {
            "circles": {
                "cid1": {"name": "Circle1", "aids": ["aid1"], "mids": ["mid1", "mid2"]},
            },
            "mem_details": {
                "mid1": {"name": "First1 Last1", "entity_picture": None},
                "mid2": {"name": "First2 Last2", "entity_picture": "EP2"},
            },
        },
    ],
)
# fmt: on
async def test_setup_entry(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    bs_setup_entry_mock: AsyncMock,
    dt_setup_entry_mock: AsyncMock,
    coordinator_mock: MagicMock,
    mem_coordinator_mock: MagicMock,
    store_data: StoreData | None,
) -> None:
    """Test config entry setup."""
    if store_data is not None:
        hass_storage[DOMAIN] = {"version": 1, "data": store_data}
        crd_data = CirclesMembersData(
            {
                CircleID(cid): CircleData.from_dict(cd)
                for cid, cd in store_data["circles"].items()
            },
            {
                MemberID(mid): MemberDetails.from_dict(md)
                for mid, md in store_data["mem_details"].items()
            },
        )
    else:
        crd_data = CirclesMembersData()
    mids = list(crd_data.mem_details)
    n_mids = len(mids)

    v2_entry = MockConfigEntry(domain=DOMAIN, version=2)
    v2_entry.add_to_hass(hass)

    crd = coordinator_mock.return_value
    crd.data = crd_data

    with assert_setup_component(0, DOMAIN):
        assert await async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done()

    coordinator_mock.assert_called_once()
    args = coordinator_mock.call_args.args
    assert len(args) == 2
    assert args[0] is hass

    store = cast(Life360Store, args[1])
    assert store.circles == crd_data.circles
    assert store.mem_details == crd_data.mem_details

    crd_1st_refresh = cast(AsyncMock, crd.async_config_entry_first_refresh)
    crd_1st_refresh.assert_called_once()
    crd_1st_refresh.assert_awaited_once()
    bs_setup_entry_mock.assert_called_once()
    dt_setup_entry_mock.assert_called_once()

    assert mem_coordinator_mock.call_count == n_mids
    for mid in mids:
        mem_coordinator_mock.assert_any_call(hass, crd, mid)
    mem_crd = mem_coordinator_mock.return_value
    mem_crd_1st_refresh = cast(AsyncMock, mem_crd.async_config_entry_first_refresh)
    assert mem_crd_1st_refresh.call_count == n_mids
    assert mem_crd_1st_refresh.await_count == n_mids


async def test_reload_entry(
    hass: HomeAssistant,
    bs_setup_entry_mock: AsyncMock,
    dt_setup_entry_mock: AsyncMock,
    coordinator_mock: MagicMock,
) -> None:
    """Test config entry reload."""
    v2_entry = MockConfigEntry(domain=DOMAIN, version=2)
    v2_entry.add_to_hass(hass)

    crd = coordinator_mock.return_value
    crd.data = CirclesMembersData()

    with assert_setup_component(0, DOMAIN):
        assert await async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done()

    coordinator_mock.reset_mock()
    bs_setup_entry_mock.reset_mock()
    dt_setup_entry_mock.reset_mock()

    assert await hass.config_entries.async_reload(v2_entry.entry_id)
    await hass.async_block_till_done()

    coordinator_mock.assert_called_once()
    crd_1st_refresh = cast(AsyncMock, crd.async_config_entry_first_refresh)
    crd_1st_refresh.assert_called_once()
    crd_1st_refresh.assert_awaited_once()
    bs_setup_entry_mock.assert_called_once()
    dt_setup_entry_mock.assert_called_once()


async def test_remove_entry(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    bs_setup_entry_mock: AsyncMock,
    dt_setup_entry_mock: AsyncMock,
    coordinator_mock: MagicMock,
) -> None:
    """Test config entry removal."""
    hass_storage[DOMAIN] = {"version": 1, "data": empty_store}

    v2_entry = MockConfigEntry(domain=DOMAIN, version=2)
    v2_entry.add_to_hass(hass)

    crd = coordinator_mock.return_value
    crd.data = CirclesMembersData()

    with assert_setup_component(0, DOMAIN):
        assert await async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done()

    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    assert DOMAIN in hass_storage

    assert await hass.config_entries.async_remove(v2_entry.entry_id)
    await hass.async_block_till_done()

    assert not hass.config_entries.async_entries(DOMAIN)
    assert DOMAIN not in hass_storage
