"""Life360 integration."""

from __future__ import annotations

import asyncio
from functools import partial
import logging
from typing import cast

try:
    from life360 import NotFound  # noqa: F401
except ImportError as err:
    raise ImportError(
        "If /config/life360 exists, remove it, restart Home Assistant, and try again"
    ) from err

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.typing import ConfigType

from .const import (
    DOMAIN,
    SERVICE_UPDATE_LOCATION,
    SIGNAL_MEMBERS_CHANGED,
    SIGNAL_UPDATE_LOCATION,
)
from .coordinator import (
    CirclesMembersDataUpdateCoordinator,
    L360ConfigEntry,
    L360Coordinators,
    MemberDataUpdateCoordinator,
)
from .helpers import Life360Store, MemberID

# Needed only if setup or async_setup exists.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_LOGGER = logging.getLogger(__name__)
_PLATFORMS = [Platform.BINARY_SENSOR, Platform.DEVICE_TRACKER]

_UPDATE_LOCATION_SCHEMA = vol.Schema(
    {vol.Required(CONF_ENTITY_ID): vol.Any(vol.All(vol.Lower, "all"), cv.entity_ids)}
)


async def async_setup(hass: HomeAssistant, _: ConfigType) -> bool:
    """Set up integration."""

    @callback
    def update_location(call: ServiceCall) -> None:
        """Request Member location update."""
        async_dispatcher_send(hass, SIGNAL_UPDATE_LOCATION, call.data[CONF_ENTITY_ID])

    hass.services.async_register(
        DOMAIN, SERVICE_UPDATE_LOCATION, update_location, _UPDATE_LOCATION_SCHEMA
    )

    return True


async def async_migrate_entry(_: HomeAssistant, entry: L360ConfigEntry) -> bool:
    """Migrate config entry."""
    # Currently, no migration is supported.
    version = str(entry.version)
    minor_version = cast(int | None, getattr(entry, "minor_version", None))
    if minor_version:
        version = f"{version}.{minor_version}"
    _LOGGER.error(
        "Unsupported configuration entry found: %s, version: %s; please remove it",
        entry.title,
        version,
    )
    return False


async def async_setup_entry(hass: HomeAssistant, entry: L360ConfigEntry) -> bool:
    """Set up config entry."""
    store = Life360Store(hass)
    await store.load()

    coordinator = CirclesMembersDataUpdateCoordinator(hass, store)
    await coordinator.async_config_entry_first_refresh()
    mem_coordinator: dict[MemberID, MemberDataUpdateCoordinator] = {}

    async def async_process_data(forward: bool = False) -> None:
        """Process Members."""
        mids = set(coordinator.data.mem_details)
        coros = [
            mem_coordinator.pop(mid).async_shutdown()
            for mid in set(mem_coordinator) - mids
        ]
        for mid in mids - set(mem_coordinator):
            entry_was = config_entries.current_entry.get()
            config_entries.current_entry.set(entry)
            mem_crd = MemberDataUpdateCoordinator(hass, coordinator, mid)
            config_entries.current_entry.set(entry_was)
            mem_coordinator[mid] = mem_crd
            coros.append(mem_crd.async_refresh())
        if coros:
            await asyncio.gather(*coros)
            if forward:
                async_dispatcher_send(hass, SIGNAL_MEMBERS_CHANGED)

    @callback
    def process_data() -> None:
        """Process Members."""
        create_process_task = partial(
            entry.async_create_background_task,
            hass,
            async_process_data(forward=True),
            "Process Members",
        )
        # eager_start parameter was added in 2024.3.
        try:
            create_process_task(eager_start=True)
        except TypeError:
            create_process_task()

    await async_process_data()
    entry.async_on_unload(coordinator.async_add_listener(process_data))
    entry.runtime_data = L360Coordinators(coordinator, mem_coordinator)

    # Set up components for our platforms.
    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: L360ConfigEntry) -> bool:
    """Unload config entry."""
    # Unload components for our platforms.
    return await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: L360ConfigEntry) -> bool:
    """Remove config entry."""
    # Don't delete store when removing old version 1 config entry.
    if entry.version < 2:
        return True
    await Life360Store(hass).remove()
    return True
