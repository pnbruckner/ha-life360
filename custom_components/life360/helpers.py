"""Life360 helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from math import ceil
from typing import Any, Self, cast

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ENABLED, CONF_PASSWORD, CONF_USERNAME, UnitOfSpeed
from homeassistant.util.unit_conversion import SpeedConverter

from .const import (
    CONF_ACCOUNTS,
    CONF_AUTHORIZATION,
    CONF_DRIVING_SPEED,
    CONF_MAX_GPS_ACCURACY,
    CONF_SHOW_DRIVING,
    CONF_VERBOSITY,
)


@dataclass
class Account:
    """Account info."""

    password: str
    authorization: str
    enabled: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Initialize from a dictionary."""
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
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Initialize from a dictionary."""
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
