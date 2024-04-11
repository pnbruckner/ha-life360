"""Life360 helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from math import ceil
from typing import Any, Self, cast

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ENABLED, CONF_PASSWORD, CONF_USERNAME

from .const import (
    CONF_ACCOUNTS,
    CONF_AUTHORIZATION,
    CONF_DRIVING_SPEED,
    CONF_MAX_GPS_ACCURACY,
    CONF_SHOW_DRIVING,
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
    driving: bool = False
    driving_speed: float | None = None
    max_gps_accuracy: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Initialize from a dictionary."""
        accts = cast(dict[str, dict[str, Any]], data[CONF_ACCOUNTS])
        return cls(
            {username: Account.from_dict(acct) for username, acct in accts.items()},
            cast(bool, data[CONF_SHOW_DRIVING]),
            cast(float | None, data[CONF_DRIVING_SPEED]),
            cast(int | None, data[CONF_MAX_GPS_ACCURACY]),
        )

    def _add_account(self, data: Mapping[str, Any], enabled: bool = True) -> None:
        """Add account."""
        self.accounts[cast(str, data[CONF_USERNAME])] = Account(
            cast(str, data[CONF_PASSWORD]), cast(str, data[CONF_AUTHORIZATION]), enabled
        )

    def _merge_options(self, data: Mapping[str, Any]) -> None:
        """Merge in options."""
        self.driving |= cast(bool, data[CONF_SHOW_DRIVING])
        if (driving_speed := cast(float | None, data[CONF_DRIVING_SPEED])) is not None:
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

    def merge_v1_config_entry(self, entry: ConfigEntry, was_enabled: bool) -> None:
        """Merge in old v1 config entry."""
        self._add_account(entry.data, was_enabled)
        self._merge_options(entry.options)
