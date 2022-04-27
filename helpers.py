"""Life360 integration helpers."""

from __future__ import annotations
from curses.ascii import SP

from typing import Any, Callable, TypedDict

from life360 import Life360, Life360Error, LoginError

from homeassistant.const import (
    ATTR_BATTERY_CHARGING,
    ATTR_BATTERY_LEVEL,
    ATTR_ENTITY_PICTURE,
    ATTR_GPS_ACCURACY,
    ATTR_LATITUDE,
    ATTR_LONGITUDE,
    ATTR_NAME,
    LENGTH_FEET,
    LENGTH_KILOMETERS,
    LENGTH_METERS,
    LENGTH_MILES,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util.distance import convert
import homeassistant.util.dt as dt_util

from .const import (
    ATTR_ADDRESS,
    ATTR_AT_LOC_SINCE,
    ATTR_DRIVING,
    ATTR_LAST_SEEN,
    ATTR_PLACE,
    ATTR_SPEED,
    ATTR_WIFI_ON,
    COMM_MAX_RETRIES,
    COMM_TIMEOUT,
    LOGGER,
    SPEED_DIGITS,
    SPEED_FACTOR_MPH,
)


class AccountData(TypedDict, total=False):
    """Account data."""

    api: Life360
    coordinator: DataUpdateCoordinator
    unsub: Callable[[], None]
    re_add_entry: bool


class IntegData(TypedDict):
    """Integration data."""

    config_options: dict[str, Any]
    accounts: dict[str, AccountData]
    tracked_members: list[str]


def get_life360_api(authorization: str | None = None) -> Life360:
    """Create Life360 api object."""
    return Life360(
        timeout=COMM_TIMEOUT, max_retries=COMM_MAX_RETRIES, authorization=authorization
    )


async def get_life360_authorization(
    hass: HomeAssistant,
    api: Life360,
    username: str,
    password: str,
    errors: dict[str, str],
) -> str | None:
    """Get Life360 authorization."""

    authorization = None

    try:
        authorization = await hass.async_add_executor_job(
            api.get_authorization, username, password
        )
    except LoginError as exc:
        LOGGER.debug("Login error: %s", exc)
        errors["base"] = "invalid_auth"
    except Life360Error as exc:
        LOGGER.debug("Unexpected error communicating with Life360 server: %s", exc)
        errors["base"] = "comm_error"

    return authorization


async def get_life360_data(
    hass: HomeAssistant, api: Life360
) -> dict[str, dict[str, Any]]:
    """Fetch data from Life360."""

    async def retrieve_data(
        func: Callable[..., list[dict[str, Any]]], *args: Any
    ) -> list[dict[str, Any]]:
        try:
            return await hass.async_add_executor_job(func, *args)
        except LoginError as exc:
            LOGGER.debug("Login error: %s", exc)
            raise ConfigEntryAuthFailed from exc
        except Life360Error as exc:
            LOGGER.debug("%s: %s", exc.__class__.__name__, exc)
            raise UpdateFailed from exc

    data: dict[str, Any] = {"circles": {}, "members": {}}

    for circle in await retrieve_data(api.get_circles):
        circle_id = circle["id"]
        circle_members = await retrieve_data(api.get_circle_members, circle_id)
        circle_places = await retrieve_data(api.get_circle_places, circle_id)
        data["circles"][circle_id] = {
            "name": circle["name"],
            "members": set(member["id"] for member in circle_members),
            "places": {
                place["id"]: {"name": place["name"]}
                | {
                    k: float(v)
                    for k, v in place.items()
                    if k in ("latitude", "longitude", "radius")
                }
                for place in circle_places
            },
        }

        for member in circle_members:
            member_id = member["id"]
            if member_id in data["members"] or not int(
                member["features"]["shareLocation"]
            ):
                continue

            loc = member["location"]

            first = member["firstName"]
            last = member["lastName"]
            if first and last:
                name = " ".join([first, last])
            else:
                name = first or last

            place = loc["name"] or None

            if place:
                address: str | None = place
            else:
                address1 = loc["address1"] or None
                address2 = loc["address2"] or None
                if address1 and address2:
                    address = ", ".join([address1, address2])
                else:
                    address = address1 or address2

            speed = float(loc["speed"]) * SPEED_FACTOR_MPH
            if hass.config.units.is_metric:
                speed = convert(speed, LENGTH_MILES, LENGTH_KILOMETERS)

            data["members"][member_id] = {
                ATTR_ADDRESS: address,
                ATTR_AT_LOC_SINCE: dt_util.utc_from_timestamp(int(loc["since"])),
                ATTR_BATTERY_CHARGING: bool(int(loc["charge"])),
                ATTR_BATTERY_LEVEL: int(float(loc["battery"])),
                ATTR_DRIVING: bool(int(loc["isDriving"])),
                ATTR_ENTITY_PICTURE: member["avatar"],
                # Life360 reports accuracy in feet, but Device Tracker expects
                # gps_accuracy in meters.
                ATTR_GPS_ACCURACY: round(
                    convert(float(loc["accuracy"]), LENGTH_FEET, LENGTH_METERS)
                ),
                ATTR_LAST_SEEN: dt_util.utc_from_timestamp(int(loc["timestamp"])),
                ATTR_LATITUDE: float(loc["latitude"]),
                ATTR_LONGITUDE: float(loc["longitude"]),
                ATTR_NAME: name,
                ATTR_PLACE: place,
                ATTR_SPEED: round(speed, SPEED_DIGITS),
                ATTR_WIFI_ON: bool(int(loc["wifiState"])),
            }

    return data
