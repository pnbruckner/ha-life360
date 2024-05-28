"""Life360 test configuration."""
from __future__ import annotations

from collections.abc import Callable, Generator, Iterable, Mapping
from functools import partial
import secrets
import string
from typing import Any, cast
from unittest.mock import MagicMock, NonCallableMagicMock, patch

from aiohttp import ClientSession
import custom_components.life360.helpers
from life360 import Life360
import pytest

from homeassistant.util import dt as dt_util

from .common import DtNowMock

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations."""
    return


@pytest.fixture(autouse=True)
def MockStoreWorkAround() -> Generator[None, None, None]:
    """Work around broken hass_storage in old pytest_homeassistant_custom_component."""
    # Versions before 0.13.54 did not use default encoder, so sets did not get converted
    # to lists, causing a JSON serialization error that was not caught. Work around the
    # problem by converting the sets to lists before writing.

    async def save(self: custom_components.life360.helpers.Life360Store) -> None:
        """Convert sets to lists in data to save."""
        data: dict[str, dict[str, dict[str, Any]]] = self.data.as_dict()
        for circle in data["circles"].values():
            circle["aids"] = list(circle["aids"])
            circle["mids"] = list(circle["mids"])
        await self._store.async_save(data)

    with patch.object(
        custom_components.life360.helpers.Life360Store, "save", autospec=True
    ) as mock:
        mock.side_effect = save
        yield


@pytest.fixture
def dt_now() -> Generator[DtNowMock, None, None]:
    """Mock util.dt.now."""
    real = dt_util.now
    with patch("homeassistant.util.dt.now", autospec=True) as mock:
        yield real, mock


Life360API_SideEffect = Iterable[Any] | Callable[..., Any]
# {API_Name: {Method: [ReturnData] | func}}
Life360API_Data = Mapping[str | None, Mapping[str, Life360API_SideEffect]]


# Pass in data using:
#
# @pytest.mark.parametrize(
#     ("MockLife360", ...),
#     [
#         {"name": {"method": [data, ...]}},
#         ...,
#     ],
#     indirect=["MockLife360"],
# )
# def test_abc(...):
#
# Any name/method not present in data will use default.
@pytest.fixture(autouse=True)
def MockLife360(request: pytest.FixtureRequest) -> Generator[MagicMock, None, None]:
    """Mock Life360."""

    if param := getattr(request, "param", None):
        api_data = cast(Life360API_Data, param)
    else:
        api_data = cast(Life360API_Data, {})

    def login_by_username(self, username: str, password: str) -> str:
        """Generate an authorization string."""
        token = "".join(
            secrets.choice(string.ascii_letters + string.digits) for i in range(48)
        )
        authorization = f"Bearer {token}"
        self.authorization = authorization
        return authorization

    def new_api(
        mock: MagicMock,
        session: ClientSession,
        max_retries: int,
        authorization: str | None = None,
        *,
        name: str | None = None,
        verbosity: int = 0,
    ) -> NonCallableMagicMock:
        """Return a new mocked Life360 instance."""
        api = NonCallableMagicMock(spec=Life360, name=name)
        api.authorization = authorization
        api.name = name
        api.verbosity = verbosity

        api_methods = api_data.get(name, {})

        method_data: Life360API_SideEffect | None
        if (method_data := api_methods.get("login_by_username")) is None:
            method_data = partial(login_by_username, api)
        api.login_by_username.side_effect = method_data
        for data_type, methods in (
            (
                dict,
                (
                    "get_me",
                    "get_circle_member",
                    "send_circle_member_request",
                    "request_circle_member_location_update",
                ),
            ),
            (list, ("get_circles", "get_circle_members")),
        ):
            for method in methods:
                if (method_data := api_methods.get(method)) is None:
                    getattr(api, method).return_value = data_type()
                else:
                    getattr(api, method).side_effect = method_data

        mock.apis.append(api)
        return api

    with patch("custom_components.life360.helpers.Life360", autospec=True) as mock:
        mock.side_effect = partial(new_api, mock)
        mock.apis = []
        yield mock
