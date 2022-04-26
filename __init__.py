"""Life360 entity-based integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, SOURCE_IMPORT
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .config_flow import account_schema
from .const import (
    CONF_ACCOUNTS,
    CONF_AUTHORIZATION,
    CONF_CIRCLES,
    CONF_DRIVING_SPEED,
    CONF_ERROR_THRESHOLD,
    CONF_MAX_GPS_ACCURACY,
    CONF_MAX_UPDATE_WAIT,
    CONF_PREFIX,
    CONF_SCAN_INTERVAL,
    CONF_MEMBERS,
    CONF_WARNING_THRESHOLD,
    DEFAULT_SCAN_INTERVAL_TD,
    DEFAULT_SCAN_INTERVAL_SEC,
    DOMAIN,
    LOGGER,
    OPTIONS,
)
from .helpers import AccountData, get_life360_api, get_life360_data, init_integ_data


PLATFORMS = [Platform.DEVICE_TRACKER]
DEFAULT_PREFIX = DOMAIN

_REMOVED = (
    CONF_CIRCLES,
    CONF_ERROR_THRESHOLD,
    CONF_MAX_UPDATE_WAIT,
    CONF_MEMBERS,
    CONF_WARNING_THRESHOLD,
)


def _prefix(value: None | str) -> None | str:
    if value == "":
        return None
    return value


def _removed(config: dict[str, Any]) -> dict[str, Any]:
    for key in list(config.keys()):
        if key in _REMOVED:
            cv.removed(key, raise_if_present=False)(config)
            config.pop(key)
    return config


LIFE360_SCHEMA = vol.Schema(
    vol.All(
        lambda x: {} if x is None else x,
        _removed,
        {
            vol.Optional(CONF_ACCOUNTS, default=list): vol.All(
                cv.ensure_list, [account_schema()]
            ),
            vol.Optional(CONF_DRIVING_SPEED): vol.Coerce(float),
            vol.Optional(CONF_MAX_GPS_ACCURACY): vol.Coerce(float),
            vol.Optional(CONF_PREFIX, default=DEFAULT_PREFIX): vol.All(
                vol.Any(None, cv.string), _prefix
            ),
            vol.Optional(
                CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL_SEC
            ): vol.Coerce(float),
        },
    )
)
CONFIG_SCHEMA = vol.Schema(
    {vol.Optional(DOMAIN, default=dict): LIFE360_SCHEMA}, extra=vol.ALLOW_EXTRA
)


def _update_interval(entry: ConfigEntry) -> timedelta:
    try:
        return timedelta(seconds=entry.options[CONF_SCAN_INTERVAL])
    except KeyError:
        return DEFAULT_SCAN_INTERVAL_TD


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up integration."""
    config = config[DOMAIN]
    options = {k: config[k] for k in OPTIONS if config.get(k) is not None}

    # LOGGER.debug("async_setup called: %s", config)

    accounts = {
        account[CONF_USERNAME].lower(): account for account in config[CONF_ACCOUNTS]
    }

    # Check existing entries against config accounts.
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.source == SOURCE_IMPORT:
            if (
                entry.unique_id not in accounts
                or entry.data[CONF_PASSWORD] != accounts[entry.unique_id][CONF_PASSWORD]
                or any(entry.options.get(k) != options.get(k) for k in OPTIONS)
            ):
                # No longer in config, or password or options have changed.
                await hass.config_entries.async_remove(entry.entry_id)
                # LOGGER.debug("Removed: %s", entry.unique_id)
            else:
                # Entry still valid, no need to create one.
                del accounts[entry.unique_id]
        elif entry.unique_id in accounts:
            account = accounts.pop(entry.unique_id)
            LOGGER.warning(
                "Skipping account %s from configuration: "
                "Credentials already configured in frontend",
                account[CONF_USERNAME],
            )

    # Initiate import config flow for any accounts in config that do not already have
    # a valid entry.
    for account in accounts.values():
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_IMPORT, "options": options},
                data=account,
            )
        )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up config entry."""
    # LOGGER.debug("__init__.async_setup_entry called: %s", entry.as_dict())

    account = init_integ_data(hass)["accounts"].setdefault(
        cast(str, entry.unique_id), AccountData()
    )

    if not (api := account.get("api")):
        api = get_life360_api(authorization=entry.data[CONF_AUTHORIZATION])
        account["api"] = api

    async def async_update_data() -> dict[str, dict[str, Any]]:
        """Update Life360 data."""

        # LOGGER.debug("async_update_data called: %s", api)
        data = await get_life360_data(hass, api)
        # LOGGER.debug("get_life360_data returned: %s", data)
        return data

    if not (coordinator := account.get("coordinator")):
        coordinator = account["coordinator"] = DataUpdateCoordinator(
            hass,
            LOGGER,
            name=f"{DOMAIN} ({entry.unique_id})",
            update_interval=_update_interval(entry),
            update_method=async_update_data,
        )

    await coordinator.async_config_entry_first_refresh()

    # Set up components for our platforms.
    hass.config_entries.async_setup_platforms(entry, PLATFORMS)

    # Add event listener for option flow changes
    entry.async_on_unload(entry.add_update_listener(async_update_options))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload config entry."""
    # LOGGER.debug("async_unload_entry called: %s", entry.unique_id)

    # Unload components for our platforms.
    # But first stop checking for new members on update.
    if (unsub := hass.data[DOMAIN]["accounts"][entry.unique_id].pop("unsub", None)) :
        unsub()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove config entry."""
    # LOGGER.debug("async_remove_entry called: %s", entry.unique_id)

    try:
        del hass.data[DOMAIN]["accounts"][entry.unique_id]
    except KeyError:
        pass


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    # LOGGER.debug("async_update_options called: %s", entry.unique_id)
    account = hass.data[DOMAIN]["accounts"][entry.unique_id]
    account["coordinator"].update_interval = _update_interval(entry)
    if account.pop("re_add_entry", False):
        await hass.config_entries.async_remove(entry.entry_id)
        await hass.config_entries.async_add(entry)
