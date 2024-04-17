"""Config flow for Life360 integration."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict
from functools import cached_property
import logging
from typing import Any, cast

from life360 import CommError, Life360Error, LoginError
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    OptionsFlowWithConfigEntry,
)
from homeassistant.const import (
    CONF_ENABLED,
    CONF_PASSWORD,
    CONF_USERNAME,
    UnitOfLength,
    UnitOfSpeed,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowHandler, FlowResult
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.util.unit_system import METRIC_SYSTEM

from . import helpers
from .const import (
    COMM_MAX_RETRIES,
    COMM_TIMEOUT,
    CONF_ACCOUNTS,
    CONF_DRIVING_SPEED,
    CONF_MAX_GPS_ACCURACY,
    CONF_SHOW_DRIVING,
    CONF_VERBOSITY,
    DOMAIN,
)
from .helpers import Account, ConfigOptions

_LOGGER = logging.getLogger(__name__)

LIMIT_GPS_ACC = "limit_gps_acc"
SET_DRIVE_SPEED = "set_drive_speed"


class Life360Flow(FlowHandler, ABC):
    """Life360 flow mixin."""

    _acct: str | None
    _username: str | None
    _password: str
    _enabled: bool

    @cached_property
    @abstractmethod
    def _opts(self) -> ConfigOptions:
        """Return mutable options class."""

    @cached_property
    def _accts(self) -> dict[str, Account]:
        """Return mutable account info."""
        return self._opts.accounts

    @cached_property
    def _speed_uom(self) -> str:
        """Return speed unit_of_measurement."""
        if self.hass.config.units is METRIC_SYSTEM:
            return UnitOfSpeed.KILOMETERS_PER_HOUR
        return UnitOfSpeed.MILES_PER_HOUR

    @property
    def _usernames(self) -> list[str]:
        """Return usernames for current accounts."""
        return list(self._accts)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Get basic options."""
        if user_input is not None:
            mga = cast(float | None, user_input.get(CONF_MAX_GPS_ACCURACY))
            self._opts.max_gps_accuracy = None if mga is None else int(mga)
            self._opts.driving_speed = cast(
                float | None, user_input.get(CONF_DRIVING_SPEED)
            )
            self._opts.driving = cast(bool, user_input[CONF_SHOW_DRIVING])
            if self.show_advanced_options:
                self._opts.verbosity = int(user_input[CONF_VERBOSITY])

            return await self.async_step_acct_menu()

        data_schema = vol.Schema(
            {
                vol.Optional(CONF_MAX_GPS_ACCURACY): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        step="any",
                        unit_of_measurement=UnitOfLength.METERS,
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(CONF_DRIVING_SPEED): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        step="any",
                        unit_of_measurement=self._speed_uom,
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(CONF_SHOW_DRIVING): BooleanSelector(),
            }
        )
        if self._opts.max_gps_accuracy is not None:
            data_schema = self.add_suggested_values_to_schema(
                data_schema, {CONF_MAX_GPS_ACCURACY: self._opts.max_gps_accuracy}
            )
        if self._opts.driving_speed is not None:
            data_schema = self.add_suggested_values_to_schema(
                data_schema, {CONF_DRIVING_SPEED: self._opts.driving_speed}
            )
        data_schema = self.add_suggested_values_to_schema(
            data_schema, {CONF_SHOW_DRIVING: self._opts.driving}
        )
        if self.show_advanced_options:
            data_schema = data_schema.extend(
                {
                    vol.Required(CONF_VERBOSITY): SelectSelector(
                        SelectSelectorConfig(
                            options=[str(i) for i in range(5)],
                            translation_key="verbosity",
                        )
                    )
                }
            )
            data_schema = self.add_suggested_values_to_schema(
                data_schema, {CONF_VERBOSITY: str(self._opts.verbosity)}
            )
        return self.async_show_form(
            step_id="init",
            data_schema=data_schema,
            last_step=False,
        )

    async def async_step_acct_menu(self, _: dict[str, Any] | None = None) -> FlowResult:
        """Handle account options."""
        if not self._accts:
            return await self.async_step_add_acct()

        menu_options = ["add_acct", "mod_acct_sel", "del_accts", "done"]
        return self.async_show_menu(
            step_id="acct_menu",
            menu_options=menu_options,
            description_placeholders={
                "accts": "\n".join(
                    [
                        f"{username}{'' if acct.enabled else ' (disabled)'}"
                        for username, acct in self._accts.items()
                    ]
                )
            },
        )

    async def async_step_add_acct(self, _: dict[str, Any] | None = None) -> FlowResult:
        """Add an account."""
        self._acct = self._username = None
        self._enabled = True
        return await self.async_step_acct()

    async def async_step_mod_acct_sel(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Select an account to modify."""
        if len(self._usernames) == 1 or user_input is not None:
            if user_input is None:
                username = self._usernames[0]
            else:
                username = cast(str, user_input[CONF_ACCOUNTS])
            self._acct = self._username = username
            self._password = self._accts[username].password
            self._enabled = self._accts[username].enabled
            return await self.async_step_acct()

        return self.async_show_form(
            step_id="mod_acct_sel",
            data_schema=self._sel_accts_schema(multiple=False),
            last_step=False,
        )

    async def async_step_acct(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Enter account credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._username = cast(str, user_input[CONF_USERNAME])
            self._password = cast(str, user_input[CONF_PASSWORD])
            self._enabled = cast(bool, user_input[CONF_ENABLED])
            try:
                await self._verify_and_save_acct()
            except vol.EmailInvalid:
                errors[CONF_USERNAME] = "invalid_email"
            except LoginError:
                errors["base"] = "invalid_auth"
            except CommError:
                errors["base"] = "cannot_connect"
            except Life360Error:
                errors["base"] = "unknown"
            else:
                return await self.async_step_acct_menu()

        data_schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.EMAIL)
                ),
                vol.Required(CONF_PASSWORD): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
                vol.Required(CONF_ENABLED): BooleanSelector(),
            }
        )
        if self._username:
            data_schema = self.add_suggested_values_to_schema(
                data_schema,
                {
                    CONF_USERNAME: self._username,
                    CONF_PASSWORD: self._password,
                },
            )
        data_schema = self.add_suggested_values_to_schema(
            data_schema, {CONF_ENABLED: self._enabled}
        )
        return self.async_show_form(
            step_id="acct",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={"action": "Modify" if self._acct else "Add"},
            last_step=False,
        )

    async def async_step_del_accts(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Delete accounts."""
        if user_input is not None:
            for acct in cast(list[str], user_input[CONF_ACCOUNTS]):
                del self._accts[acct]
            return await self.async_step_acct_menu()

        return self.async_show_form(
            step_id="del_accts",
            data_schema=self._sel_accts_schema(multiple=True),
            last_step=False,
        )

    def _sel_accts_schema(self, multiple: bool) -> vol.Schema:
        """Create data schema to select one, or possibly more, account(s)."""
        data_schema = vol.Schema(
            {
                vol.Required(CONF_ACCOUNTS): SelectSelector(
                    SelectSelectorConfig(options=self._usernames, multiple=multiple),
                )
            }
        )
        if multiple:
            return self.add_suggested_values_to_schema(data_schema, {CONF_ACCOUNTS: []})
        return data_schema

    async def _verify_and_save_acct(self) -> None:
        """Verify and save account to options."""
        # Validate email address.
        self._username = cast(str, vol.Email()(self._username))

        # Check that credentials work by getting new authorization.
        if self._enabled:
            session = async_create_clientsession(self.hass, timeout=COMM_TIMEOUT)
            try:
                api = helpers.Life360(
                    session, COMM_MAX_RETRIES, verbosity=self._opts.verbosity
                )
                authorization = await api.login_by_username(
                    self._username, self._password
                )
            finally:
                session.detach()
        else:
            # No point in keeping old authorization, if there was one, because once
            # account is re-enabled, a new authorization will be obtained.
            authorization = ""

        if self._acct and self._username != self._acct:
            del self._accts[self._acct]
        self._accts[self._username] = Account(
            self._password, authorization, self._enabled
        )

    @abstractmethod
    async def async_step_done(self, _: dict[str, Any] | None = None) -> FlowResult:
        """Finish the flow."""


class Life360ConfigFlow(ConfigFlow, Life360Flow, domain=DOMAIN):
    """Life360 integration config flow."""

    VERSION = 2

    @cached_property
    def _opts(self) -> ConfigOptions:
        """Return mutable options class."""
        return ConfigOptions()

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> Life360OptionsFlow:
        """Get the options flow for this handler."""
        # Default first step is init.
        return Life360OptionsFlow(config_entry)

    # When HA versions before 2024.4 are dropped, return types should be changed from
    # FlowResult to ConfigFlowResult.
    async def async_step_user(  # type: ignore[override]
        self, _: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle a config flow initiated by the user."""
        # manifest.json single_config_entry option added in 2024.3. Once versions before
        # that are no longer supported, this check can be removed.
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        return await self.async_step_init()

    async def async_step_done(self, _: dict[str, Any] | None = None) -> FlowResult:
        """Finish the flow."""
        return self.async_create_entry(
            title="Life360", data={}, options=asdict(self._opts)
        )


class Life360OptionsFlow(OptionsFlowWithConfigEntry, Life360Flow):
    """Life360 integration options flow."""

    @cached_property
    def _opts(self) -> ConfigOptions:
        """Return mutable options class."""
        return ConfigOptions.from_dict(self.options)

    async def async_step_done(self, _: dict[str, Any] | None = None) -> FlowResult:
        """Finish the flow."""
        return self.async_create_entry(title="", data=asdict(self._opts))
