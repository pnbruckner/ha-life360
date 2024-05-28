"""Life360 integration."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import asdict
from functools import partial
import logging
from typing import Any, cast

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import SOURCE_USER, ConfigEntry, ConfigEntryDisabler
from homeassistant.const import CONF_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.typing import ConfigType
from homeassistant.util.unit_system import METRIC_SYSTEM

from .const import (
    DOMAIN,
    SERVICE_UPDATE_LOCATION,
    SIGNAL_MEMBERS_CHANGED,
    SIGNAL_UPDATE_LOCATION,
)
from .coordinator import (
    CirclesMembersDataUpdateCoordinator,
    MemberDataUpdateCoordinator,
)
from .helpers import ConfigOptions, Life360Store, MemberID

# Needed only if setup or async_setup exists.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_LOGGER = logging.getLogger(__name__)
_PLATFORMS = [Platform.BINARY_SENSOR, Platform.DEVICE_TRACKER]

_UPDATE_LOCATION_SCHEMA = vol.Schema(
    {vol.Required(CONF_ENTITY_ID): vol.Any(vol.All(vol.Lower, "all"), cv.entity_ids)}
)


def _migrate_entity(
    ent_reg: er.EntityRegistry, entity: er.RegistryEntry, config_entry_id: str
) -> None:
    """Migrate an entity registry entry to new config entry."""
    kwargs: dict[str, Any] = {"config_entry_id": config_entry_id}
    # Entity may have been disabled indirectly when old V1 config entries were disabled
    # below prior to completing migration in separate task. If so, re-enable them.
    if entity.disabled_by is er.RegistryEntryDisabler.CONFIG_ENTRY:
        kwargs["disabled_by"] = None
    ent_reg.async_update_entity(entity.entity_id, **kwargs)


async def _finish_migration(
    hass: HomeAssistant,
    v2_entry: ConfigEntry,
    entities: Iterable[er.RegistryEntry],
    entries: Iterable[ConfigEntry],
) -> None:
    """Finish migration."""
    ent_reg = er.async_get(hass)

    # Add new v2 config entry.
    await hass.config_entries.async_add(v2_entry)

    # Migrate entity registry entries to new config entry.
    for entity in entities:
        _migrate_entity(ent_reg, entity, v2_entry.entry_id)

    # Remove old v1 entries.
    for entry in entries:
        await hass.config_entries.async_remove(entry.entry_id)


async def _migrate_config_entries(
    hass: HomeAssistant, entries: Iterable[ConfigEntry]
) -> None:
    """Migrate config entries from version 1 -> 2."""
    ent_reg = er.async_get(hass)

    # Get data from existing version 1 config entries.
    options = ConfigOptions()
    entities: list[er.RegistryEntry] = []

    for entry in entries:
        # Make sure entry is disabled so it won't get set up before we get a chance to
        # remove it.
        if was_enabled := not entry.disabled_by:
            await hass.config_entries.async_set_disabled_by(
                entry.entry_id, ConfigEntryDisabler.USER
            )

        # Convert data & options to options for new config entry.
        options.merge_v1_config_entry(
            entry, was_enabled, hass.config.units is METRIC_SYSTEM
        )

        # Gather entities so they can be reassigned to new config entry.
        entities.extend(er.async_entries_for_config_entry(ent_reg, entry.entry_id))

    # Create new config entry.
    # minor_version is new in 2024.1.
    create_config_entry = partial(
        ConfigEntry,
        version=2,
        domain=DOMAIN,
        title="Life360",
        data={},
        source=SOURCE_USER,
        options=asdict(options),
    )
    try:
        v2_entry = create_config_entry(minor_version=1)
    except TypeError:
        v2_entry = create_config_entry()

    # Cannot add a new config entry here since we're called from async_setup, which is
    # called from setup.async_setup_component, which is at the point where our domain
    # has not yet been added to hass.config.components (since async_setup hasn't
    # finished yet.)
    # If we did add a new config entry, config_entries.async_add would call its
    # async_setup method with the new entry, which would see our domain is not in
    # hass.config.components, so would call setup.async_setup_component, which would
    # start an infinite loop.
    # So, finish the remaining operations, including adding the new config entry, in a
    # separate task.
    hass.async_create_task(_finish_migration(hass, v2_entry, entities, entries))


async def async_setup(hass: HomeAssistant, _: ConfigType) -> bool:
    """Set up integration."""
    # Migrate from version 1 - > 2 if necessary.
    if entries := hass.config_entries.async_entries(DOMAIN):
        version_1_seen = False
        version_2_entry: ConfigEntry | None = None
        for entry in entries:
            match entry.version:
                case 1:
                    version_1_seen = True
                case 2:
                    assert version_2_entry is None
                    version_2_entry = entry
                case _:
                    version = str(entry.version)
                    minor_version = cast(
                        int | None, getattr(entry, "minor_version", None)
                    )
                    if minor_version:
                        version = f"{version}.{minor_version}"
                    _LOGGER.error(
                        "Unsupported configuration entry found: %s, version: %s",
                        entry.title,
                        version,
                    )
                    return False

        if version_1_seen:
            if version_2_entry:
                # Migration was aborted while in progress.

                # Make sure entity registry entries are associated with version 2
                # config entry.
                ent_reg = er.async_get(hass)
                for entity in er.async_get(hass).entities.values():
                    _migrate_entity(ent_reg, entity, version_2_entry.entry_id)

                # Remove old entries.
                for entry in entries:
                    if entry.version == 2:
                        continue
                    await hass.config_entries.async_remove(entry.entry_id)
            else:
                _LOGGER.warning(
                    "Migrating Life360 integration entries from version 1 to 2"
                )
                await _migrate_config_entries(hass, entries)

    @callback
    def update_location(call: ServiceCall) -> None:
        """Request Member location update."""
        async_dispatcher_send(hass, SIGNAL_UPDATE_LOCATION, call.data[CONF_ENTITY_ID])

    hass.services.async_register(
        DOMAIN, SERVICE_UPDATE_LOCATION, update_location, _UPDATE_LOCATION_SCHEMA
    )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
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
            coros.append(mem_crd.async_config_entry_first_refresh())
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
    hass.data[DOMAIN] = {"coordinator": coordinator, "mem_coordinator": mem_coordinator}

    # Set up components for our platforms.
    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload config entry."""
    # Unload components for our platforms.
    result = await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)
    del hass.data[DOMAIN]
    return result


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Remove config entry."""
    # Don't delete store when migrating from version 1 config entry.
    if entry.version == 1:
        return True
    await Life360Store(hass).remove()
    return True
