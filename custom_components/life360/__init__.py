"""Life360 integration."""

from __future__ import annotations

from collections.abc import Callable

import voluptuous as vol

from homeassistant.components.device_tracker import CONF_SCAN_INTERVAL
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_EXCLUDE,
    CONF_INCLUDE,
    CONF_PASSWORD,
    CONF_PREFIX,
    CONF_USERNAME,
    Platform,
)
from homeassistant.core import HomeAssistant
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_CIRCLES,
    CONF_DRIVING_SPEED,
    CONF_ERROR_THRESHOLD,
    CONF_MAX_GPS_ACCURACY,
    CONF_MAX_UPDATE_WAIT,
    CONF_MEMBERS,
    CONF_SHOW_AS_STATE,
    CONF_WARNING_THRESHOLD,
    DATA_CONFIG_OPTIONS,
    DEFAULT_OPTIONS,
    DOMAIN,
    LOGGER,
    SHOW_DRIVING,
    SHOW_MOVING,
)
from .coordinator import (
    Life360DataUpdateCoordinator,
    async_unloading_life360_config_entry,
    init_life360_coordinator,
)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.DEVICE_TRACKER]

CONF_ACCOUNTS = "accounts"

SHOW_AS_STATE_OPTS = [SHOW_DRIVING, SHOW_MOVING]
UNSUPPORTED_CONFIG_OPTIONS = {
    CONF_ACCOUNTS,
    CONF_CIRCLES,
    CONF_ERROR_THRESHOLD,
    CONF_MAX_UPDATE_WAIT,
    CONF_MEMBERS,
    CONF_PREFIX,
    CONF_SCAN_INTERVAL,
    CONF_WARNING_THRESHOLD,
}


def _show_as_state(config: dict) -> dict:
    if opts := config.pop(CONF_SHOW_AS_STATE):
        if SHOW_DRIVING in opts:
            config[SHOW_DRIVING] = True
        if SHOW_MOVING in opts:
            LOGGER.warning(
                "%s is no longer supported as an option for %s",
                SHOW_MOVING,
                CONF_SHOW_AS_STATE,
            )
    return config


def _unsupported(unsupported: set[str]) -> Callable[[dict], dict]:
    """Warn about unsupported options and remove from config."""

    def validator(config: dict) -> dict:
        if unsupported_keys := unsupported & set(config):
            LOGGER.warning(
                "The following options are no longer supported: %s",
                ", ".join(sorted(unsupported_keys)),
            )
        return {k: v for k, v in config.items() if k not in unsupported}

    return validator


ACCOUNT_SCHEMA = {
    vol.Required(CONF_USERNAME): cv.string,
    vol.Required(CONF_PASSWORD): cv.string,
}
CIRCLES_MEMBERS = {
    vol.Optional(CONF_EXCLUDE): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional(CONF_INCLUDE): vol.All(cv.ensure_list, [cv.string]),
}
LIFE360_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Optional(CONF_ACCOUNTS): vol.All(cv.ensure_list, [ACCOUNT_SCHEMA]),
            vol.Optional(CONF_CIRCLES): CIRCLES_MEMBERS,
            vol.Optional(CONF_DRIVING_SPEED): vol.Coerce(float),
            vol.Optional(CONF_ERROR_THRESHOLD): vol.Coerce(int),
            vol.Optional(CONF_MAX_GPS_ACCURACY): vol.Coerce(float),
            vol.Optional(CONF_MAX_UPDATE_WAIT): cv.time_period,
            vol.Optional(CONF_MEMBERS): CIRCLES_MEMBERS,
            vol.Optional(CONF_PREFIX): vol.Any(None, cv.string),
            vol.Optional(CONF_SCAN_INTERVAL): cv.time_period,
            vol.Optional(CONF_SHOW_AS_STATE, default=[]): vol.All(
                cv.ensure_list, [vol.In(SHOW_AS_STATE_OPTS)]
            ),
            vol.Optional(CONF_WARNING_THRESHOLD): vol.Coerce(int),
        }
    ),
    _unsupported(UNSUPPORTED_CONFIG_OPTIONS),
    _show_as_state,
)
CONFIG_SCHEMA = vol.Schema(
    vol.All({DOMAIN: LIFE360_SCHEMA}, cv.removed(DOMAIN, raise_if_present=False)),
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up integration."""
    hass.data[DOMAIN] = {}
    hass.data[DOMAIN][DATA_CONFIG_OPTIONS] = config.get(DOMAIN, {})

    init_life360_coordinator(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up config entry."""
    # Check if this entry was created when this was a "legacy" tracker (i.e., before
    # 2022.7.) If it was, update with missing data.
    if not entry.unique_id:
        hass.config_entries.async_update_entry(
            entry,
            unique_id=entry.data[CONF_USERNAME].lower(),
            options=DEFAULT_OPTIONS | hass.data[DOMAIN][DATA_CONFIG_OPTIONS],
        )

    # Config specific coordinator will register itself with central coordinator.
    await Life360DataUpdateCoordinator(hass).async_refresh()

    # Set up components for our platforms.
    hass.config_entries.async_setup_platforms(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload config entry."""
    await async_unloading_life360_config_entry(hass, entry)

    # Unload components for our platforms.
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
