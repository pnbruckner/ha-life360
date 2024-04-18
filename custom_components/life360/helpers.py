"""Life360 helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import IntEnum
from math import ceil
from typing import Any, NewType, Self, cast

from life360 import Life360

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_ENABLED,
    CONF_PASSWORD,
    CONF_USERNAME,
    UnitOfLength,
    UnitOfSpeed,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.restore_state import ExtraStoredData
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import DistanceConverter, SpeedConverter

from .const import (
    CONF_ACCOUNTS,
    CONF_AUTHORIZATION,
    CONF_DRIVING_SPEED,
    CONF_MAX_GPS_ACCURACY,
    CONF_SHOW_DRIVING,
    CONF_VERBOSITY,
    DOMAIN,
    SPEED_DIGITS,
    SPEED_FACTOR_MPH,
)

# So testing can patch in one place.
LIFE360 = Life360


@dataclass
class Account:
    """Account info."""

    password: str
    authorization: str
    enabled: bool = True

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        """Initialize from a dictionary.

        Raises KeyError if any data is missing.
        """
        return cls(data[CONF_PASSWORD], data[CONF_AUTHORIZATION], data[CONF_ENABLED])


@dataclass
class ConfigOptions:
    """Config entry options."""

    accounts: dict[str, Account] = field(default_factory=dict)
    # CONF_SHOW_DRIVING is actually "driving" for legacy reasons.
    driving: bool = False
    driving_speed: float | None = None
    max_gps_accuracy: int | None = None
    verbosity: int = 0

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        """Initialize from a dictionary.

        Raises KeyError if any data is missing.
        """
        accts = cast(dict[str, dict[str, Any]], data[CONF_ACCOUNTS])
        return cls(
            {username: Account.from_dict(acct) for username, acct in accts.items()},
            data[CONF_SHOW_DRIVING],
            data[CONF_DRIVING_SPEED],
            data[CONF_MAX_GPS_ACCURACY],
            data[CONF_VERBOSITY],
        )

    def _add_account(self, data: Mapping[str, Any], enabled: bool = True) -> None:
        """Add account."""
        self.accounts[data[CONF_USERNAME]] = Account(
            data[CONF_PASSWORD], data[CONF_AUTHORIZATION], enabled
        )

    def _merge_options(self, data: Mapping[str, Any], metric: bool) -> None:
        """Merge in options."""
        self.driving |= data[CONF_SHOW_DRIVING]
        if (driving_speed := data[CONF_DRIVING_SPEED]) is not None:
            # Life360 reports speed in MPH, so we'll save driving speed threshold in
            # that unit. However, previously the value stored in the config entry was in
            # the current HA unit system, so we need to convert if that was (is) KPH.
            if metric:
                driving_speed = SpeedConverter.convert(
                    driving_speed,
                    UnitOfSpeed.KILOMETERS_PER_HOUR,
                    UnitOfSpeed.MILES_PER_HOUR,
                )
            if self.driving_speed is None:
                self.driving_speed = driving_speed
            else:
                self.driving_speed = min(self.driving_speed, driving_speed)
        if (max_gps_accuracy := data[CONF_MAX_GPS_ACCURACY]) is not None:
            mga_int = ceil(max_gps_accuracy)
            if self.max_gps_accuracy is None:
                self.max_gps_accuracy = mga_int
            else:
                self.max_gps_accuracy = max(self.max_gps_accuracy, mga_int)

    def merge_v1_config_entry(
        self, entry: ConfigEntry, was_enabled: bool, metric: bool
    ) -> None:
        """Merge in old v1 config entry."""
        self._add_account(entry.data, was_enabled)
        self._merge_options(entry.options, metric)


@dataclass
class LocationDetails:
    """Life360 Member location details."""

    address: str | None
    at_loc_since: datetime
    driving: bool
    gps_accuracy: int  # meters
    last_seen: datetime
    latitude: float
    longitude: float
    place: str | list[str] | None
    speed: float  # mph

    @staticmethod
    def to_datetime(value: Any) -> datetime:
        """Extract value at key and convert to datetime.

        Raises ValueError if value is not a valid datetime or representation of one.
        """
        if isinstance(value, datetime):
            return dt_util.as_local(value)
        try:
            parsed_value = dt_util.parse_datetime(value)
        except TypeError:
            raise ValueError from None
        if parsed_value is None:
            raise ValueError
        return dt_util.as_local(parsed_value)

    @classmethod
    def from_dict(cls, restored: Mapping[str, Any]) -> Self:
        """Initialize from a dictionary.

        Raises KeyError if any data is missing.
        Raises ValueError if any data is malformed.
        """
        return cls(
            restored["address"],
            cls.to_datetime(restored["at_loc_since"]),
            restored["driving"],
            restored["gps_accuracy"],
            cls.to_datetime(restored["last_seen"]),
            restored["latitude"],
            restored["longitude"],
            restored["place"],
            restored["speed"],
        )

    @classmethod
    def from_server(cls, raw_loc: Mapping[str, Any]) -> Self:
        """Initialize from Member's location data from server."""
        address1 = raw_loc["address1"] or None
        address2 = raw_loc["address2"] or None
        if address1 and address2:
            address: str | None = ", ".join([address1, address2])
        else:
            address = address1 or address2

        return cls(
            address,
            dt_util.as_local(dt_util.utc_from_timestamp(int(raw_loc["since"]))),
            bool(int(raw_loc["isDriving"])),
            # Life360 reports accuracy in feet, but Device Tracker expects
            # gps_accuracy in meters.
            round(
                DistanceConverter.convert(
                    float(raw_loc["accuracy"]), UnitOfLength.FEET, UnitOfLength.METERS
                )
            ),
            dt_util.as_local(dt_util.utc_from_timestamp(int(raw_loc["timestamp"]))),
            float(raw_loc["latitude"]),
            float(raw_loc["longitude"]),
            raw_loc["name"] or None,
            round(max(0, float(raw_loc["speed"]) * SPEED_FACTOR_MPH), SPEED_DIGITS),
        )


@dataclass
class LocationData:
    """Life360 Member location data."""

    details: LocationDetails
    battery_charging: bool
    battery_level: int
    wifi_on: bool

    @classmethod
    def from_dict(cls, restored: Mapping[str, Any]) -> Self:
        """Initialize from a dictionary.

        Raises KeyError if any data is missing.
        Raises ValueError if any data is malformed.
        """
        return cls(
            LocationDetails.from_dict(restored["details"]),
            restored["battery_charging"],
            restored["battery_level"],
            restored["wifi_on"],
        )

    @classmethod
    def from_server(cls, raw_loc: Mapping[str, Any]) -> Self:
        """Initialize from Member's location data from server."""
        return cls(
            LocationDetails.from_server(raw_loc),
            bool(int(raw_loc["charge"])),
            int(float(raw_loc["battery"])),
            bool(int(raw_loc["wifiState"])),
        )


class NoLocReason(IntEnum):
    """Reason why Member location data is missing."""

    EXPLICIT = 2
    NO_REASON = 1
    NOT_SHARING = 0
    NOT_SET = -1


@dataclass
class MemberData(ExtraStoredData):
    """Life360 Member data."""

    name: str
    entity_picture: str | None
    loc: LocationData | None = None
    loc_missing: NoLocReason = NoLocReason.NOT_SET
    err_msg: str | None = field(default=None, compare=False)

    def as_dict(self) -> dict[str, Any]:
        """Return a dict representation of the data."""
        return asdict(self)

    @classmethod
    def from_dict(cls, restored: Mapping[str, Any]) -> Self:
        """Initialize from a dictionary.

        Raises KeyError if any data is missing.
        Raises ValueError if any data is malformed.
        """
        if restored_loc := restored["loc"]:
            loc = LocationData.from_dict(restored_loc)
        else:
            loc = None
        return cls(
            restored["name"],
            restored["entity_picture"],
            loc,
            NoLocReason(restored["loc_missing"]),
            restored["err_msg"],
        )

    @classmethod
    def from_server(cls, raw_member: Mapping[str, Any]) -> Self:
        """Initialize from Member's data from server."""
        first = raw_member["firstName"]
        last = raw_member["lastName"]
        if first and last:
            name = f"{first} {last}"
        else:
            name = first or last or "No Name"
        entity_picture = raw_member["avatar"]

        if not int(raw_member["features"]["shareLocation"]):
            # Member isn't sharing location with this Circle.
            return cls(name, entity_picture, loc_missing=NoLocReason.NOT_SHARING)

        if not (raw_loc := raw_member["location"]):
            if err_msg := raw_member["issues"]["title"]:
                if extended_reason := raw_member["issues"]["dialog"]:
                    err_msg = f"{err_msg}: {extended_reason}"
                loc_missing = NoLocReason.EXPLICIT
            else:
                err_msg = (
                    "The user may have lost connection to Life360. "
                    "See https://www.life360.com/support/"
                )
                loc_missing = NoLocReason.NO_REASON
            return cls(name, entity_picture, loc_missing=loc_missing, err_msg=err_msg)

        return cls(name, entity_picture, LocationData.from_server(raw_loc))

    # Since a Member can exist in more than one Circle, and the data retrieved for the
    # Member might be different in each (e.g., some might not share location info but
    # others do), provide a means to find the "best" data for the Member from a list of
    # data, one from each Circle. Implementing the __lt__ method is all that is needed
    # for the built-in sorted function.
    def __lt__(self, other: MemberData) -> bool:
        """Determine if this member should sort before another."""
        if not self.loc:
            return other.loc is not None or self.loc_missing < other.loc_missing
        if not other.loc:
            return False
        return self.loc.details.last_seen < other.loc.details.last_seen


CircleID = NewType("CircleID", str)
MemberID = NewType("MemberID", str)
Members = dict[MemberID, MemberData]


@dataclass
class CircleData:
    """Circle data."""

    name: str
    usernames: list[str] = field(default_factory=list)
    mids: list[MemberID] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        """Initialize from a dictionary."""
        return cls(data["name"], data["usernames"], data["mids"])


@dataclass
class StoredData:
    """Life360 storage data."""

    circles: dict[CircleID, CircleData] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Return a dict representation of the data."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        """Initialize from a dictionary."""
        return cls(
            {
                cid: CircleData.from_dict(circle_data)
                for cid, circle_data in data["circles"].items()
            }
        )


class Life360Store:
    """Life360 storage."""

    _loaded_ok: bool = False
    data: StoredData

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize storage."""
        self._store = Store[dict[str, Any]](hass, 1, DOMAIN)

    @property
    def loaded_ok(self) -> bool:
        """Return if load succeeded."""
        return self._loaded_ok

    @property
    def circles(self) -> dict[CircleID, CircleData]:
        """Return circles."""
        return self.data.circles

    @circles.setter
    def circles(self, circles: dict[CircleID, CircleData]) -> None:
        """Update circles."""
        self.data.circles = circles

    async def load(self) -> bool:
        """Load from storage.

        Should be called once, before data is accessed.
        Returns True if store was read ok.
        Initializes data and returns False otherwise.
        Also sets loaded_ok accordingly.
        """
        if store_data := await self._store.async_load():
            self.data = StoredData.from_dict(store_data)
            self._loaded_ok = True
        else:
            self.data = StoredData()
        return self._loaded_ok

    async def save(self) -> None:
        """Write to storage."""
        await self._store.async_save(self.data.as_dict())
