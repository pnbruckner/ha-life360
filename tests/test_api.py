"""Tests for the Life360 API wrapper."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.life360.api import Life360
from life360 import LoginError
import pytest


def make_session() -> MagicMock:
    """Create a mocked aiohttp session."""
    session = MagicMock()
    session.timeout = SimpleNamespace(total=60)
    session.cookie_jar = MagicMock()
    return session


@pytest.mark.asyncio
async def test_request_switches_to_curl_after_login_error() -> None:
    """Test that a 403 switches future requests to curl_cffi."""
    api = Life360(make_session(), 0, authorization="Bearer token")
    api._curl_request = AsyncMock(side_effect=[{"value": 1}, {"value": 2}])

    with patch(
        "life360.api.Life360._request",
        new=AsyncMock(side_effect=LoginError("forbidden")),
    ) as mock_request:
        assert await api._request("https://example.com/one", False) == {"value": 1}
        assert await api._request("https://example.com/two", False) == {"value": 2}

    assert mock_request.await_count == 1
    assert api._curl_request.await_count == 2
    await api.async_close()


@pytest.mark.asyncio
async def test_request_keeps_login_error_if_curl_fallback_fails() -> None:
    """Test that the original login error is preserved if curl also gets a 403."""
    api = Life360(make_session(), 0, authorization="Bearer token")
    api._curl_request = AsyncMock(side_effect=LoginError("still forbidden"))

    with patch(
        "life360.api.Life360._request",
        new=AsyncMock(side_effect=LoginError("forbidden")),
    ):
        with pytest.raises(LoginError, match="forbidden"):
            await api._request("https://example.com/one", False)

    assert api._prefer_curl is False
    await api.async_close()


@pytest.mark.asyncio
async def test_clear_cookies_clears_both_transports() -> None:
    """Test cookie clearing for the aiohttp transport."""
    session = make_session()
    api = Life360(session, 0, authorization="Bearer token")
    api.clear_cookies()

    session.cookie_jar.clear.assert_called_once_with()
    await api.async_close()


class FakeProcess:
    """Test double for asyncio subprocesses."""

    def __init__(
        self, stdout: bytes, stderr: bytes = b"", returncode: int = 0
    ) -> None:
        """Initialize the fake process."""
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self, _input: bytes | None = None) -> tuple[bytes, bytes]:
        """Return captured process output."""
        return self._stdout, self._stderr


@pytest.mark.asyncio
async def test_curl_request_returns_json() -> None:
    """Test curl subprocess success path."""
    api = Life360(make_session(), 0, authorization="Bearer token")
    api._curl_bin = "/usr/bin/curl"
    response = b'{"circles":[{"id":"1"}]}\n__L360_HTTP_STATUS__:200'

    with patch(
        "custom_components.life360.api.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=FakeProcess(response)),
    ) as mock_exec:
        assert await api._curl_request("https://example.com/circles", False) == {
            "circles": [{"id": "1"}]
        }

    assert mock_exec.await_count == 1
    await api.async_close()


@pytest.mark.asyncio
async def test_curl_request_maps_403_to_login_error() -> None:
    """Test curl subprocess maps HTTP 403 to LoginError."""
    api = Life360(make_session(), 0, authorization="Bearer token")
    api._curl_bin = "/usr/bin/curl"
    response = b"forbidden\n__L360_HTTP_STATUS__:403"

    with patch(
        "custom_components.life360.api.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=FakeProcess(response)),
    ):
        with pytest.raises(LoginError, match="forbidden"):
            await api._curl_request("https://example.com/circles", False)

    await api.async_close()
