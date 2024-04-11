"""Life360 integration."""

from __future__ import annotations

from functools import partial
import logging
from math import ceil
from typing import Any, cast

from homeassistant.config_entries import SOURCE_USER, ConfigEntry, ConfigEntryDisabler
from homeassistant.const import CONF_ENABLED, CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_ACCOUNTS,
    CONF_AUTHORIZATION,
    CONF_CIRCLES,
    CONF_DRIVING_SPEED,
    CONF_MAX_GPS_ACCURACY,
    CONF_SHOW_DRIVING,
    DOMAIN,
    STORAGE_KEY,
    STORAGE_VERSION,
)

# Needed only if setup or async_setup exists.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_LOGGER = logging.getLogger(__name__)
_PLATFORMS = [Platform.BINARY_SENSOR, Platform.DEVICE_TRACKER]


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


async def _migrate_config_entries(
    hass: HomeAssistant, entries: list[ConfigEntry]
) -> None:
    """Migrate config entries from version 1 -> 2."""
    ent_reg = er.async_get(hass)

    # Get data from existing version 1 config entries.
    cfg_accts: dict[str, dict[str, Any]] = {}
    str_accts: dict[str, dict[str, Any]] = {}
    entities: list[er.RegistryEntry] = []
    show_driving = False
    driving_speed: float | None = None
    max_gps_accuracy: int | None = None

    for entry in entries:
        # Make sure entry is disabled so it won't get set up before we get a chance to
        # remove it.
        if was_enabled := not entry.disabled_by:
            await hass.config_entries.async_set_disabled_by(
                entry.entry_id, ConfigEntryDisabler.USER
            )

        username = cast(str, entry.data[CONF_USERNAME])

        # Split config entry data. Password goes into new config entry's options, and
        # authorization goes into (new) .storage file.
        cfg_accts[username] = {
            CONF_PASSWORD: cast(str, entry.data[CONF_PASSWORD]),
            CONF_ENABLED: was_enabled,
        }
        str_accts[username] = {
            CONF_AUTHORIZATION: cast(str, entry.data[CONF_AUTHORIZATION]),
            CONF_CIRCLES: [],
        }

        # Gather entities so they can be reassigned to new config entry.
        entities.extend(er.async_entries_for_config_entry(ent_reg, entry.entry_id))

        # Combine remaining options since we'll only have one set now.
        if cast(bool, entry.options[CONF_SHOW_DRIVING]):
            show_driving = True
        if (
            entry_driving_speed := cast(float | None, entry.options[CONF_DRIVING_SPEED])
        ) is not None:
            if driving_speed is None:
                driving_speed = entry_driving_speed
            else:
                driving_speed = min(driving_speed, entry_driving_speed)
        if (
            entry_max_gps_accuracy := cast(
                float | None, entry.options[CONF_MAX_GPS_ACCURACY]
            )
        ) is not None:
            entry_mga_int = ceil(entry_max_gps_accuracy)
            if max_gps_accuracy is None:
                max_gps_accuracy = entry_mga_int
            else:
                max_gps_accuracy = max(max_gps_accuracy, entry_mga_int)

    # Create .storage file. Make it private since it contains authorization string.
    store = Store[dict[str, dict[str, dict[str, Any]]]](
        hass, STORAGE_VERSION, STORAGE_KEY, private=True
    )
    await store.async_save({CONF_ACCOUNTS: str_accts})

    # Create new config entry.
    # minor_version is new in 2024.1.
    create_config_entry = partial(
        ConfigEntry,
        version=2,
        domain=DOMAIN,
        title="Life360",
        data={},
        source=SOURCE_USER,
        options={
            CONF_ACCOUNTS: cfg_accts,
            CONF_SHOW_DRIVING: show_driving,
            CONF_MAX_GPS_ACCURACY: max_gps_accuracy,
            CONF_DRIVING_SPEED: driving_speed,
        },
    )
    try:
        v2_entry = create_config_entry(minor_version=1)
    except TypeError:
        v2_entry = create_config_entry()

    async def finish_migration(v2_entry: ConfigEntry) -> None:
        """Finish migration."""
        # Add new v2 config entry.
        await hass.config_entries.async_add(v2_entry)

        # Migrate entity registry entries to new config entry.
        for entity in entities:
            _migrate_entity(ent_reg, entity, v2_entry.entry_id)

        # Remove old v1 entries.
        for entry in entries:
            await hass.config_entries.async_remove(entry.entry_id)

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
    hass.async_create_task(finish_migration(v2_entry))


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

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up config entry."""
    # TODO: Create update_coordinator, ...???
    # TODO: Monitor config entry updates and adjust .storage, etc. according to account
    #       changes.

    # Set up components for our platforms.
    # await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload config entry."""
    # Unload components for our platforms.
    # return await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)
    return True
