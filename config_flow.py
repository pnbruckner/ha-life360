"""Config flow to configure Life360 integration."""

from __future__ import annotations

from asyncio import sleep
from collections.abc import Mapping
from typing import Any, Callable, cast, Coroutine

from life360 import Life360
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    OptionsFlow,
    SOURCE_IMPORT,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import (
    AbortFlow,
    FlowResult,
    RESULT_TYPE_ABORT,
    RESULT_TYPE_CREATE_ENTRY,
    RESULT_TYPE_FORM,
)
import homeassistant.helpers.config_validation as cv

from .const import (
    CONF_AUTHORIZATION,
    CONF_DRIVING_SPEED,
    CONF_MAX_GPS_ACCURACY,
    CONF_PREFIX,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL_SEC,
    DOMAIN,
    LOGGER,
    OPTIONS,
)
from .helpers import AccountData, get_life360_api, get_life360_authorization


_IMPORT_RETRY_PERIOD = 10
_IMPORT_ERRORS = {
    "already_configured": "Already configured",
    "invalid_auth": "Invalid credentials",
}


def account_schema(
    def_username: str | vol.UNDEFINED = vol.UNDEFINED,
    def_password: str | vol.UNDEFINED = vol.UNDEFINED,
) -> dict[Any, Callable[[Any], str]]:
    """Return schema for an account with optional default values."""
    return {
        vol.Required(CONF_USERNAME, default=def_username): cv.string,
        vol.Required(CONF_PASSWORD, default=def_password): cv.string,
    }


def password_schema(
    def_password: str | vol.UNDEFINED = vol.UNDEFINED,
) -> dict[Any, Callable[[Any], str]]:
    """Return schema for a password with optional default value."""
    return {vol.Required(CONF_PASSWORD, default=def_password): cv.string}


class Life360ConfigFlow(ConfigFlow, domain=DOMAIN):
    """Life360 integration config flow."""

    VERSION = 2

    def __init__(self) -> None:
        """Initialize."""
        self._username: str | vol.UNDEFINED = vol.UNDEFINED
        self._password: str | vol.UNDEFINED = vol.UNDEFINED
        self._api: Life360 | None = None
        self._reauth_entry: ConfigEntry | None = None
        self._first_reauth_confirm = True

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> Life360OptionsFlow:
        """Get the options flow for this handler."""
        return Life360OptionsFlow(entry)

    @classmethod
    @callback
    def async_supports_options_flow(cls, entry: ConfigEntry) -> bool:
        """Return options flow support for this handler."""
        return entry.source != SOURCE_IMPORT

    @property
    def _options(self) -> Mapping[str, Any]:
        """Options for new config entry."""
        return cast(Mapping[str, Any], self.context.setdefault("options", {}))

    @_options.setter
    def _options(self, options: Mapping[str, Any]) -> None:
        self.context["options"] = options

    async def _async_verify(self, step_id: str) -> FlowResult:
        """Attempt to authorize the provided credentials."""

        assert self._api
        assert self._username
        assert self._password

        errors: dict[str, str] = {}
        authorization = await get_life360_authorization(
            self.hass, self._api, self._username, self._password, errors
        )
        if errors:
            if step_id == "user":
                schema = account_schema(
                    self._username, self._password
                ) | _account_options_schema(self._options)
            else:
                schema = password_schema(self._password)
            return self.async_show_form(
                step_id=step_id, data_schema=vol.Schema(schema), errors=errors
            )

        data = {
            CONF_USERNAME: self._username,
            CONF_PASSWORD: self._password,
            CONF_AUTHORIZATION: authorization,
        }

        if self._reauth_entry:
            LOGGER.info("Reauthorization successful")
            self.hass.config_entries.async_update_entry(self._reauth_entry, data=data)
            if self._reauth_entry.state == ConfigEntryState.LOADED:
                # Config entry reload should not be necessary. Restarting coordinator's
                # scheduled refreshes should be sufficient since Life360 api object is
                # valid again after successful reauthorization.
                coordinator = self.hass.data[DOMAIN]["accounts"][self.unique_id][
                    "coordinator"
                ]
                self.hass.async_create_task(coordinator.async_request_refresh())
            else:
                # Config entry never got completely loaded, so do a full reload.
                self.hass.async_create_task(
                    self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
                )
            return self.async_abort(reason="reauth_successful")

        self.hass.data[DOMAIN]["accounts"][cast(str, self.unique_id)] = AccountData(
            api=self._api
        )
        title = cast(str, self.unique_id)
        if self.source == SOURCE_IMPORT:
            title += " (from configuration)"
        return self.async_create_entry(title=title, data=data, options=self._options)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle a config flow initiated by the user."""
        if not user_input:
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema(
                    account_schema() | _account_options_schema(self._options)
                ),
            )

        self._options = _extract_account_options(user_input)

        self._username = user_input[CONF_USERNAME]
        self._password = user_input[CONF_PASSWORD]

        await self.async_set_unique_id(self._username.lower())
        self._abort_if_unique_id_configured()

        if not self._api:
            self._api = get_life360_api()

        return await self._async_verify("user")

    async def async_step_import(self, user_input: dict[str, Any]) -> FlowResult:
        """Handle a config flow from configuration."""
        return await self._import(self.async_step_user, user_input | self._options)

    async def async_step_reauth(self, user_input: dict[str, Any]) -> FlowResult:
        """Handle reauthorization."""
        self._username = user_input[CONF_USERNAME]
        self._api = self.hass.data[DOMAIN]["accounts"][self.unique_id]["api"]
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        # Always start with current credentials since they may still be valid and a
        # simple reauthorization will be successful.
        if cast(ConfigEntry, self._reauth_entry).source == SOURCE_IMPORT:
            return await self._import(self.async_step_reauth_confirm, user_input)
        return await self.async_step_reauth_confirm(user_input)

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle reauthorization completion."""
        if not user_input:
            # Don't show current password the first time we prompt for password since
            # this will happen asynchronously. However, once the user enters a password,
            # we can show it in case it's not valid to make it easier to enter a long,
            # complicated password.
            pwd = vol.UNDEFINED if self._first_reauth_confirm else self._password
            self._first_reauth_confirm = False
            return self.async_show_form(
                step_id="reauth_confirm", data_schema=vol.Schema(password_schema(pwd))
            )

        self._password = user_input[CONF_PASSWORD]
        return await self._async_verify("reauth_confirm")

    async def _import(
        self,
        step_func: Callable[[dict[str, str]], Coroutine[None, None, FlowResult]],
        user_input: dict[str, str],
    ) -> FlowResult:
        """Handle config flows from configuration."""
        while True:
            try:
                result = await step_func(user_input)
            except AbortFlow as exc:
                reason = exc.reason
            else:
                if result["type"] in (RESULT_TYPE_ABORT, RESULT_TYPE_CREATE_ENTRY):
                    assert (
                        result["type"] == RESULT_TYPE_CREATE_ENTRY
                        or result["reason"] == "reauth_successful"
                    )
                    return result

                assert result["type"] == RESULT_TYPE_FORM

                reason = cast(dict[str, str], result["errors"])["base"]

            if error := _IMPORT_ERRORS.get(reason):
                LOGGER.error(
                    "Problem with account %s from configuration: %s",
                    self._username,
                    error,
                )
                return self.async_abort(reason=reason)

            assert reason == "comm_error"

            LOGGER.warning(
                "Could not authenticate account %s from configuration, will try again",
                self._username,
            )
            await sleep(_IMPORT_RETRY_PERIOD)


class Life360OptionsFlow(OptionsFlow):
    """Life360 integration options flow."""

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize."""
        self.entry = entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle account options."""
        options = self.entry.options

        if user_input is not None:
            user_input = _extract_account_options(user_input)
            # If prefix has changed then tell __init__.async_update_options() to remove
            # and re-add config entry.
            self.hass.data[DOMAIN]["accounts"][self.entry.unique_id][
                "re_add_entry"
            ] = user_input.get(CONF_PREFIX) != options.get(CONF_PREFIX)
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init", data_schema=vol.Schema(_account_options_schema(options))
        )


def _account_options_schema(options: Mapping[str, Any]) -> vol.Schema:
    """Create schema for account options form."""
    def_use_prefix = CONF_PREFIX in options
    def_prefix = options.get(CONF_PREFIX, DOMAIN)
    def_limit_gps_acc = CONF_MAX_GPS_ACCURACY in options
    def_max_gps = options.get(CONF_MAX_GPS_ACCURACY, vol.UNDEFINED)
    def_set_drive_speed = CONF_DRIVING_SPEED in options
    def_speed = options.get(CONF_DRIVING_SPEED, vol.UNDEFINED)
    def_scan_interval = options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_SEC)

    return {
        vol.Required("use_prefix", default=def_use_prefix): bool,
        vol.Optional(CONF_PREFIX, default=def_prefix): str,
        vol.Required("limit_gps_acc", default=def_limit_gps_acc): bool,
        vol.Optional(CONF_MAX_GPS_ACCURACY, default=def_max_gps): vol.Coerce(float),
        vol.Required("set_drive_speed", default=def_set_drive_speed): bool,
        vol.Optional(CONF_DRIVING_SPEED, default=def_speed): vol.Coerce(float),
        vol.Optional(CONF_SCAN_INTERVAL, default=def_scan_interval): vol.Coerce(float),
    }


def _extract_account_options(user_input: dict) -> dict[str, Any]:
    """Remove options from user input and return as a separate dict."""
    result = {}

    for key in OPTIONS:
        value = user_input.pop(key, None)
        # Was "include" checkbox (if there was one) corresponding to option key True
        # (meaning option should be included)?
        incl = user_input.pop(
            {
                CONF_PREFIX: "use_prefix",
                CONF_MAX_GPS_ACCURACY: "limmit_gps_acc",
                CONF_DRIVING_SPEED: "set_drive_speed",
            }.get(key),
            True,
        )
        if incl and value is not None:
            result[key] = value

    return result
