"""Shared base class & setup helper for Life360 per-Member entities."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence

from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION, DOMAIN, SIGNAL_MEMBERS_CHANGED
from .coordinator import L360ConfigEntry, MemberDataUpdateCoordinator
from .helpers import MemberData, MemberID

_LOGGER = logging.getLogger(__name__)


def member_device_info(mid: MemberID, name: str) -> DeviceInfo:
    """Return DeviceInfo for a Member.

    Used by the device tracker and all per-Member sensors so they group together
    under one device.
    """
    return DeviceInfo(identifiers={(DOMAIN, mid)}, name=f"Life360 {name}")


class Life360MemberEntity(CoordinatorEntity[MemberDataUpdateCoordinator]):
    """Base class for per-Member sensors."""

    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True

    def __init__(
        self, coordinator: MemberDataUpdateCoordinator, mid: MemberID, key: str
    ) -> None:
        """Initialize entity."""
        super().__init__(coordinator)
        self._mid = mid
        self._attr_unique_id = f"{mid}-{key}"
        self._attr_device_info = member_device_info(mid, coordinator.data.details.name)

    @property
    def member_data(self) -> MemberData:
        """Return the Member's current data."""
        return self.coordinator.data


def setup_member_entities(
    hass: HomeAssistant,
    entry: L360ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    factory: Callable[
        [MemberDataUpdateCoordinator, MemberID], Sequence[Life360MemberEntity]
    ],
) -> None:
    """Create/remove per-Member entities as Members come and go.

    Mirrors the way the device tracker platform handles Members.
    """
    coordinator = entry.runtime_data.coordinator
    mem_coordinator = entry.runtime_data.mem_coordinator
    entities: dict[MemberID, Sequence[Life360MemberEntity]] = {}

    async def async_process_data() -> None:
        """Process Members."""
        mids = set(coordinator.data.mem_details)
        del_mids = set(entities) - mids
        add_mids = mids - set(entities)

        if del_mids:
            old_entities = [e for mid in del_mids for e in entities.pop(mid)]
            _LOGGER.debug("Deleting %d per-Member entities", len(old_entities))
            await asyncio.gather(*(e.async_remove() for e in old_entities if e.enabled))

        if add_mids:
            new_entities: list[Life360MemberEntity] = []
            for mid in add_mids:
                entities[mid] = factory(mem_coordinator[mid], mid)
                new_entities.extend(entities[mid])
            _LOGGER.debug("Adding %d per-Member entities", len(new_entities))
            async_add_entities(new_entities)

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_MEMBERS_CHANGED, async_process_data)
    )
