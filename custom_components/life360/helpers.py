"""Life360 helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import IntEnum
from math import ceil
from typing import Any, Self, cast

from life360 import Life360

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_ENABLED,
    CONF_PASSWORD,
    CONF_USERNAME,
    UnitOfLength,
    UnitOfSpeed,
)
from homeassistant.helpers.restore_state import ExtraStoredData
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import DistanceConverter, SpeedConverter

from .const import (
    CONF_ACCOUNTS,
    CONF_AUTHORIZATION,
    CONF_DRIVING_SPEED,
    CONF_MAX_GPS_ACCURACY,
    CONF_SHOW_DRIVING,
    CONF_VERBOSITY,
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
            cast(bool, data[CONF_SHOW_DRIVING]),
            cast(float | None, data[CONF_DRIVING_SPEED]),
            cast(int | None, data[CONF_MAX_GPS_ACCURACY]),
            cast(bool, data[CONF_VERBOSITY]),
        )

    def _add_account(self, data: Mapping[str, Any], enabled: bool = True) -> None:
        """Add account."""
        self.accounts[cast(str, data[CONF_USERNAME])] = Account(
            cast(str, data[CONF_PASSWORD]), cast(str, data[CONF_AUTHORIZATION]), enabled
        )

    def _merge_options(self, data: Mapping[str, Any], metric: bool) -> None:
        """Merge in options."""
        self.driving |= cast(bool, data[CONF_SHOW_DRIVING])
        if (driving_speed := cast(float | None, data[CONF_DRIVING_SPEED])) is not None:
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
        if (
            max_gps_accuracy := cast(float | None, data[CONF_MAX_GPS_ACCURACY])
        ) is not None:
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


class MemberStatus(IntEnum):
    """Status of dynamic member data."""

    VALID = 3
    MISSING_W_REASON = 2
    MISSING_NO_REASON = 1
    NOT_SHARING = 0


@dataclass(frozen=True)
class MiscData:
    """Life360 Member miscellaneous data."""

    name: str
    entity_picture: str | None
    status: MemberStatus = MemberStatus.VALID
    err_msg: str | None = field(default=None, compare=False)

    @classmethod
    def from_dict(cls, restored: Mapping[str, Any]) -> Self:
        """Initialize MiscData from a dict.

        Raises KeyError if any data is missing.
        """
        return cls(
            restored["name"],
            restored["entity_picture"],
            MemberStatus(restored["status"]),
            restored["err_msg"],
        )

    @classmethod
    def from_server(
        cls, raw_member: Mapping[str, Any]
    ) -> tuple[Self, dict[str, Any] | None]:
        """Initialize MiscData from Member's data from server.

        Returns MiscData, and Member's location data (if any) from server.
        """
        first = cast(str | None, raw_member["firstName"])
        last = cast(str | None, raw_member["lastName"])
        if first and last:
            name = f"{first} {last}"
        else:
            name = first or last or "No Name"
        entity_picture = cast(str | None, raw_member["avatar"])

        if not int(cast(str, raw_member["features"]["shareLocation"])):
            # Member isn't sharing location with this Circle.
            return cls(name, entity_picture, status=MemberStatus.NOT_SHARING), None

        if not (raw_loc := cast(dict[str, Any] | None, raw_member["location"])):
            if err_msg := cast(str | None, raw_member["issues"]["title"]):
                if extended_reason := cast(str | None, raw_member["issues"]["dialog"]):
                    err_msg = f"{err_msg}: {extended_reason}"
                status = MemberStatus.MISSING_W_REASON
            else:
                err_msg = (
                    "The user may have lost connection to Life360. "
                    "See https://www.life360.com/support/"
                )
                status = MemberStatus.MISSING_NO_REASON
            return cls(name, entity_picture, status, err_msg), None

        return cls(name, entity_picture), raw_loc


@dataclass(frozen=True)
class LocationDetails:
    """Life360 Member location details."""

    address: str | None
    at_loc_since: datetime
    driving: bool
    gps_accuracy: int  # meters
    last_seen: datetime
    latitude: float
    longitude: float
    place: str | None
    speed: float  # mph

    @staticmethod
    def to_datetime(value: Any) -> datetime:
        """Extract value at key and convert to datetime.

        Raises ValueError if value is not a valid datetime or representation of one.
        """
        if isinstance(value, datetime):
            return value
        try:
            parsed_value = dt_util.parse_datetime(value)
        except TypeError:
            raise ValueError from None
        if parsed_value is None:
            raise ValueError
        return parsed_value

    @classmethod
    def from_dict(cls, restored: Mapping[str, Any]) -> Self | None:
        """Initialize LocationDetails from a dict.

        Returns None if any data is missing or malformed.
        """
        try:
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
        except (KeyError, ValueError):
            return None

    @classmethod
    def from_server(cls, raw_loc: Mapping[str, Any]) -> Self:
        """Initialize LocationDetails from Member's location data from server."""
        address1 = cast(str | None, raw_loc["address1"]) or None
        address2 = cast(str | None, raw_loc["address2"]) or None
        if address1 and address2:
            address: str | None = ", ".join([address1, address2])
        else:
            address = address1 or address2

        return cls(
            address,
            dt_util.utc_from_timestamp(int(cast(str, raw_loc["since"]))),
            bool(int(cast(str, raw_loc["isDriving"]))),
            # Life360 reports accuracy in feet, but Device Tracker expects
            # gps_accuracy in meters.
            round(
                DistanceConverter.convert(
                    float(cast(str, raw_loc["accuracy"])),
                    UnitOfLength.FEET,
                    UnitOfLength.METERS,
                )
            ),
            dt_util.utc_from_timestamp(int(cast(str, raw_loc["timestamp"]))),
            float(cast(str, raw_loc["latitude"])),
            float(cast(str, raw_loc["longitude"])),
            cast(str, raw_loc["name"]) or None,
            round(
                max(0, float(cast(str, raw_loc["speed"])) * SPEED_FACTOR_MPH),
                SPEED_DIGITS,
            ),
        )


@dataclass(frozen=True)
class LocationData:
    """Life360 Member location data."""

    details: LocationDetails
    battery_charging: bool
    battery_level: int
    wifi_on: bool

    @classmethod
    def from_dict(cls, restored: Mapping[str, Any]) -> Self | None:
        """Initialize LocationData from a dict.

        Returns None if any data is missing or malformed.
        """
        if (details := LocationDetails.from_dict(restored)) is None:
            return None
        try:
            return cls(
                details,
                restored["battery_charging"],
                restored["battery_level"],
                restored["wifi_on"],
            )
        except KeyError:
            return None

    @classmethod
    def from_server(cls, raw_loc: Mapping[str, Any]) -> Self:
        """Initialize LocationData from Member's location data from server."""
        return cls(
            LocationDetails.from_server(raw_loc),
            bool(int(cast(str, raw_loc["charge"]))),
            int(float(cast(str, raw_loc["battery"]))),
            bool(int(cast(str, raw_loc["wifiState"]))),
        )


@dataclass(frozen=True)
class MemberData(ExtraStoredData):
    """Life360 Member data."""

    misc: MiscData
    loc: LocationData | None

    def as_dict(self) -> dict[str, Any]:
        """Return a dict representation of the data."""
        return asdict(self)

    @classmethod
    def from_dict(cls, restored: Mapping[str, Any]) -> Self:
        """Initialize MemberData from a dict.

        Raises KeyError if any miscellaneous data is missing.
        loc will be None if any location data is missing or malformed.
        """
        return cls(MiscData.from_dict(restored), LocationData.from_dict(restored))

    @classmethod
    def from_server(cls, raw_member: Mapping[str, Any]) -> Self:
        """Initialize MemberData from Member's data from server."""
        misc, raw_loc = MiscData.from_server(raw_member)
        if raw_loc is None:
            return cls(misc, None)
        return cls(misc, LocationData.from_server(raw_loc))

    # Since a Member can exist in more than one Circle, and the data retrieved for the
    # Member might be different in each (e.g., some might not share location info but
    # others do), provide a means to find the "best" data for the Member from a list of
    # data, one from each Circle.
    def __lt__(self, other: MemberData) -> bool:
        """Determine if this member should sort before another."""
        if self.misc.status < other.misc.status:
            return True
        if not (self.misc.status == other.misc.status == MemberStatus.VALID):
            return False
        assert self.loc and other.loc
        if not self.loc.details.place and other.loc.details.place:
            return True
        return self.loc.details.last_seen < other.loc.details.last_seen


# MemberID = NewType("MemberID", str)
# Members = dict[MemberID, MemberData]
# CircleID = NewType("CircleID", str)
