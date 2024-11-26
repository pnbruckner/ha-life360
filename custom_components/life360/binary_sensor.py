"""Life360 Binary Sensor."""

from __future__ import annotations

import asyncio
from functools import cached_property, partial
import logging
from typing import cast

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ATTRIBUTION, SIGNAL_ACCT_STATUS
from .coordinator import CirclesMembersDataUpdateCoordinator, L360ConfigEntry
from .helpers import AccountID, ConfigOptions

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: L360ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the binary sensory platform."""
    coordinator = entry.runtime_data.coordinator
    entities: dict[AccountID, Life360BinarySensor] = {}

    async def process_config(hass: HomeAssistant, entry: L360ConfigEntry) -> None:
        """Add and/or remove binary online sensors."""
        options = ConfigOptions.from_dict(entry.options)
        aids = set(options.accounts)
        cur_aids = set(entities)
        del_aids = cur_aids - aids
        add_aids = aids - cur_aids

        if del_aids:
            old_entities = [entities.pop(aid) for aid in del_aids]
            _LOGGER.debug("Deleting binary online sensors for: %s", ", ".join(del_aids))
            await asyncio.gather(*(entity.async_remove() for entity in old_entities))

        if add_aids:
            new_entities = {
                aid: Life360BinarySensor(coordinator, aid) for aid in add_aids
            }
            entities.update(new_entities)
            _LOGGER.debug("Adding binary online sensors for: %s", ", ".join(add_aids))
            async_add_entities(new_entities.values())

    await process_config(hass, entry)
    entry.async_on_unload(entry.add_update_listener(process_config))


class Life360BinarySensor(BinarySensorEntity):
    """Life360 Binary Sensor."""

    _attr_attribution = ATTRIBUTION
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_should_poll = False

    def __init__(
        self, coordinator: CirclesMembersDataUpdateCoordinator, aid: AccountID
    ) -> None:
        """Initialize binary sensor."""
        self._attr_name = f"Life360 online ({aid})"
        self._attr_unique_id = aid
        self._enabled = (
            ConfigOptions.from_dict(coordinator.config_entry.options)
            .accounts[aid]
            .enabled
        )
        self._online = partial(coordinator.acct_online, aid)

        self.async_on_remove(
            coordinator.config_entry.add_update_listener(
                self._async_config_entry_updated
            )
        )

    @cached_property
    def aid(self) -> AccountID:
        """Return account ID."""
        return cast(AccountID, self.unique_id)

    @property
    def is_on(self) -> bool:
        """Return if account is online."""
        if not self._enabled:
            return False
        return self._online()

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""

        @callback
        def write_state(aid: AccountID) -> None:
            """Write state if account status was updated."""
            if aid == self.aid:
                self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_ACCT_STATUS, write_state)
        )

    async def _async_config_entry_updated(
        self, _: HomeAssistant, entry: L360ConfigEntry
    ) -> None:
        """Run when the config entry has been updated."""
        enabled = ConfigOptions.from_dict(entry.options).accounts[self.aid].enabled
        if enabled == self._enabled:
            return

        self._enabled = enabled
        self.async_write_ha_state()
