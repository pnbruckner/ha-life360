"""Life360 API wrapper with a curl-backed fallback."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from json import JSONDecodeError
import json
import logging
from shutil import which
from typing import Any, cast
from urllib.parse import urlencode

from life360 import CommError, Life360Error, LoginError, NotFound, NotModified
from life360 import RateLimited, Unauthorized
from life360.api import (
    _EXC_REPR_REDACTIONS,
    _HEADERS,
    _RESP_REPR_REDACTIONS,
    _RESP_TEXT_ALL_REDACTIONS,
    _RESP_TEXT_BASIC_REDACTIONS,
    _URL_REDACTIONS,
    Life360 as BaseLife360,
    _format_exc,
)
from life360.const import HTTP_Error

_LOGGER = logging.getLogger(__name__)
_CURL_STATUS_MARKER = "__L360_HTTP_STATUS__:"
_CURL_BINARIES = (which("curl"), "/usr/bin/curl", "/usr/local/bin/curl", "/bin/curl")

_CURL_RETRY_STATUS_CODES = frozenset(
    {
        HTTP_Error.BAD_GATEWAY,
        HTTP_Error.SERVICE_UNAVAILABLE,
        HTTP_Error.GATEWAY_TIME_OUT,
        HTTP_Error.SERVER_UNKNOWN_ERROR,
    }
)


class Life360(BaseLife360):
    """Upstream Life360 client with a curl_cffi fallback for Cloudflare 403s."""

    def __init__(
        self,
        session,
        max_retries: int,
        authorization: str | None = None,
        *,
        name: str | None = None,
        verbosity: int = 0,
    ) -> None:
        """Initialize API."""
        super().__init__(
            session,
            max_retries,
            authorization,
            name=name,
            verbosity=verbosity,
        )
        self._curl_timeout = getattr(getattr(session, "timeout", None), "total", None)
        self._curl_bin = next((path for path in _CURL_BINARIES if path), None)
        self._prefer_curl = False
        self._warned_missing_curl = False

    def clear_cookies(self) -> None:
        """Clear cookies kept by both transports."""
        self._session.cookie_jar.clear()

    async def async_close(self) -> None:
        """Close resources used by the fallback session."""
        return None

    async def _request(
        self,
        url: str,
        /,
        raise_not_modified: bool,
        method: str = "get",
        *,
        authorization: str | None = None,
        **kwargs: dict[str, Any],
    ) -> Any:
        """Make a request to server."""
        if self._prefer_curl:
            return await self._curl_request(
                url,
                raise_not_modified,
                method,
                authorization=authorization,
                **kwargs,
            )

        try:
            return await super()._request(
                url,
                raise_not_modified,
                method,
                authorization=authorization,
                **kwargs,
            )
        except LoginError as exc:
            try:
                result = await self._curl_request(
                    url,
                    raise_not_modified,
                    method,
                    authorization=authorization,
                    **kwargs,
                )
            except Life360Error as curl_exc:
                self._logger.warning(
                    "curl fallback failed for %s after aiohttp 403: %s",
                    self._redact(url, _URL_REDACTIONS),
                    type(curl_exc).__name__,
                )
                raise exc

            self._prefer_curl = True
            self._logger.warning(
                "Switching to curl subprocess transport after aiohttp 403 for %s",
                self._redact(url, _URL_REDACTIONS),
            )
            return result

    async def _curl_request(
        self,
        url: str,
        /,
        raise_not_modified: bool,
        method: str = "get",
        *,
        authorization: str | None = None,
        **kwargs: dict[str, Any],
    ) -> Any:
        """Make a request to server using the curl CLI."""
        if authorization is None:
            authorization = self.authorization
        if authorization is None:
            raise LoginError("Must login")
        if not self._curl_bin:
            if not self._warned_missing_curl:
                self._warned_missing_curl = True
                self._logger.warning("curl fallback unavailable: curl executable not found")
            raise CommError("curl executable not found", None)

        headers: dict[str, str] = {}
        if authorization != "":
            headers["authorization"] = authorization
        if raise_not_modified and (etag := self._etags.get(url)):
            headers["if-none-match"] = etag
        headers = _HEADERS | headers | kwargs.pop("headers", {})

        body: bytes | None = None
        if json_data := kwargs.pop("json", None):
            headers.setdefault("content-type", "application/json")
            body = json.dumps(json_data).encode()
        elif data := kwargs.pop("data", None):
            if isinstance(data, (bytes, bytearray)):
                body = bytes(data)
            elif isinstance(data, str):
                body = data.encode()
            else:
                headers.setdefault("content-type", "application/x-www-form-urlencoded")
                body = urlencode(data).encode()

        if params := kwargs.pop("params", None):
            encoded = urlencode(params, doseq=True)
            url = f"{url}{'&' if '?' in url else '?'}{encoded}"

        cmd = [
            self._curl_bin,
            "--silent",
            "--show-error",
            "--location",
            "--compressed",
            "--request",
            method.upper(),
            "--url",
            url,
            "--write-out",
            f"\n{_CURL_STATUS_MARKER}%{{http_code}}",
        ]
        if self._curl_timeout is not None:
            cmd.extend(["--max-time", str(self._curl_timeout)])
        for key, value in headers.items():
            cmd.extend(["--header", f"{key}: {value}"])
        if body is not None:
            cmd.extend(["--data-binary", "@-"])

        for attempt in range(1, self._max_attempts + 1):
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE if body is not None else None,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate(body)
            except OSError as exc:
                if attempt < self._max_attempts:
                    continue
                raise CommError(
                    self._redact(_format_exc(exc), _URL_REDACTIONS), None
                ) from exc

            text, _, status_str = stdout.decode(errors="replace").rpartition(
                f"\n{_CURL_STATUS_MARKER}"
            )
            status = int(status_str) if status_str.isdigit() else None

            if proc.returncode != 0 and status is None:
                self._logger.debug(
                    "Curl fallback request error: %s(%s), attempt %i: %s",
                    method.upper(),
                    self._redact(url, _URL_REDACTIONS),
                    attempt,
                    self._redact(stderr.decode(errors="replace"), _EXC_REPR_REDACTIONS),
                )
                if attempt < self._max_attempts:
                    continue
                raise CommError(
                    self._redact(stderr.decode(errors="replace"), _URL_REDACTIONS)
                    or "curl request failed",
                    None,
                ) from None

            self._dump_curl_resp(status, text)
            if status == HTTP_Error.NOT_MODIFIED:
                raise NotModified
            if status in _CURL_RETRY_STATUS_CODES and attempt < self._max_attempts:
                continue
            if status is None:
                raise CommError("curl request returned no HTTP status", None)
            if status >= 400:
                raise self._curl_error(status, text)

            try:
                return json.loads(text)
            except JSONDecodeError as exc:
                self._logger.debug(
                    "While parsing curl fallback response: %r: %s",
                    text,
                    self._redact(repr(exc), _EXC_REPR_REDACTIONS),
                )
                raise Life360Error(
                    self._redact(_format_exc(exc), _URL_REDACTIONS)
                ) from None

        raise Life360Error("Unexpected curl request flow")

    def _curl_error(self, status: int, text: str) -> Life360Error:
        """Convert a curl HTTP status and response body to the library's exceptions."""
        headers: dict[str, str] = {}
        err_msg = text
        try:
            err_msg = cast(dict[str, str], json.loads(text))["errorMessage"].lower()
        except (KeyError, TypeError, ValueError, JSONDecodeError):
            pass

        match status:
            case HTTP_Error.UNAUTHORIZED:
                raise Unauthorized(err_msg, headers.get("www-authenticate"))
            case HTTP_Error.FORBIDDEN:
                raise LoginError(err_msg)
            case HTTP_Error.NOT_FOUND:
                raise NotFound(err_msg)
            case HTTP_Error.TOO_MANY_REQUESTS:
                retry_after = headers.get("retry-after")
                try:
                    retry_after_value = float(retry_after) if retry_after else None
                except ValueError:
                    retry_after_value = None
                raise RateLimited(err_msg, retry_after_value)
            case _:
                raise CommError(err_msg, status)

    def _dump_curl_resp(self, status: int | None, text: str) -> None:
        """Dump curl fallback response to log."""
        if self.verbosity >= 1:
            self._logger.debug(
                "Curl fallback response status: %s",
                status,
            )
        if self.verbosity >= 2 and text:
            self._logger.debug(
                "Curl fallback response data: %s",
                self._redact(
                    text,
                    _RESP_TEXT_ALL_REDACTIONS
                    if self.verbosity < 3
                    else _RESP_TEXT_BASIC_REDACTIONS,
                ),
            )
