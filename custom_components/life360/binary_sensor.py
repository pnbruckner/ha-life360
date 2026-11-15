"""Life360 Binary Sensor."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import cast, override

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from propcache.api import cached_property

from .const import ATTRIBUTION, SIGNAL_ACCT_STATUS
from .coordinator import (
    CirclesMembersDataUpdateCoordinator,
    L360ConfigEntry,
    MemberDataUpdateCoordinator,
)
from .entity import Life360MemberEntity, setup_member_entities
from .helpers import AccountID, ConfigOptions, MemberData, MemberID

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

    # Per-Member binary sensors.
    setup_member_entities(
        hass,
        entry,
        async_add_entities,
        lambda crd, mid: [
            Life360MemberBinarySensor(crd, mid, desc) for desc in MEMBER_SENSORS
        ],
    )


class Life360BinarySensor(BinarySensorEntity):
    """Life360 Binary Sensor."""

    _attr_attribution = ATTRIBUTION
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "online"

    def __init__(
        self, coordinator: CirclesMembersDataUpdateCoordinator, aid: AccountID
    ) -> None:
        """Initialize binary sensor."""
        self._attr_translation_placeholders = {"acct_id": aid}
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
    @override
    def is_on(self) -> bool:
        """Return if account is online."""
        if not self._enabled:
            return False
        return self._online()

    @override
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
        # Check to make sure account hasn't just been deleted. If so, don't update state
        # because we're about to be removed.
        if not (acct := ConfigOptions.from_dict(entry.options).accounts.get(self.aid)):
            return
        if acct.enabled == self._enabled:
            return

        self._enabled = not self._enabled
        self.async_write_ha_state()


def _driving(data: MemberData, options: ConfigOptions) -> bool | None:
    """Return if driving, honoring the "driving speed threshold" option."""
    if not data.loc:
        return None
    if (
        options.driving_speed is not None
        and data.loc.details.speed >= options.driving_speed
    ):
        return True
    return data.loc.details.driving


@dataclass(frozen=True, kw_only=True)
class Life360MemberBinarySensorDescription(BinarySensorEntityDescription):
    """Life360 per-Member binary sensor description."""

    value_fn: Callable[[MemberData, ConfigOptions], bool | None]


MEMBER_SENSORS: tuple[Life360MemberBinarySensorDescription, ...] = (
    Life360MemberBinarySensorDescription(
        key="drv",
        translation_key="drv",
        icon="mdi:car",
        value_fn=_driving,
    ),
    Life360MemberBinarySensorDescription(
        key="bat_chg",
        translation_key="bat_chg",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        value_fn=lambda d, _: d.loc.battery_charging if d.loc else None,
    ),
    Life360MemberBinarySensorDescription(
        key="wifi_on",
        translation_key="wifi_on",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=lambda d, _: d.loc.wifi_on if d.loc else None,
    ),
)


class Life360MemberBinarySensor(Life360MemberEntity, BinarySensorEntity):
    """Life360 per-Member binary sensor."""

    entity_description: Life360MemberBinarySensorDescription

    def __init__(
        self,
        coordinator: MemberDataUpdateCoordinator,
        mid: MemberID,
        description: Life360MemberBinarySensorDescription,
    ) -> None:
        """Initialize binary sensor."""
        super().__init__(coordinator, mid, description.key)
        self.entity_description = description
        self._options = ConfigOptions.from_dict(coordinator.config_entry.options)
        self.async_on_remove(
            coordinator.config_entry.add_update_listener(
                self._async_config_entry_updated
            )
        )

    @property
    @override
    def is_on(self) -> bool | None:
        """Return if sensor is on."""
        return self.entity_description.value_fn(self.member_data, self._options)

    async def _async_config_entry_updated(
        self, _: HomeAssistant, entry: L360ConfigEntry
    ) -> None:
        """Re-evaluate when options (e.g., driving speed threshold) change."""
        if self._options == (new_options := ConfigOptions.from_dict(entry.options)):
            return
        self._options = new_options
        self.async_write_ha_state()
