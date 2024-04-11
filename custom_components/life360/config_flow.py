"""Config flow for Life360 integration."""

from __future__ import annotations

from abc import abstractmethod
from functools import cached_property
import logging
from typing import Any, cast

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    OptionsFlowWithConfigEntry,
)
from homeassistant.const import CONF_ENABLED, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowHandler, FlowResult
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import CONF_ACCOUNTS, DOMAIN

_LOGGER = logging.getLogger(__name__)

LIMIT_GPS_ACC = "limit_gps_acc"
SET_DRIVE_SPEED = "set_drive_speed"


class Life360Flow(FlowHandler):
    """Life360 flow mixin."""

    _acct: str | None
    _username: str | None
    _password: str

    @property
    @abstractmethod
    def options(self) -> dict[str, Any]:
        """Return mutable copy of options."""

    @cached_property
    def _accts(self) -> dict[str, dict[str, Any]]:
        """Return current account info."""
        return cast(dict[str, dict[str, Any]], self.options[CONF_ACCOUNTS])

    @property
    def _usernames(self) -> list[str]:
        """Return usernames for current accounts."""
        return list(self._accts)

    async def async_step_init(self, _: dict[str, Any] | None = None) -> FlowResult:
        """Handle account options."""
        menu_options = ["add_acct"]
        if self._accts:
            menu_options.extend(["mod_acct_sel", "del_accts", "max_gps_acc"])
        return self.async_show_menu(
            step_id="init",
            menu_options=menu_options,
            description_placeholders={"accts": "\n".join(self._usernames)},
        )

    async def async_step_add_acct(self, _: dict[str, Any] | None = None) -> FlowResult:
        """Add an account."""
        self._acct = self._username = None
        return await self.async_step_acct()

    async def async_step_mod_acct_sel(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Select an account to modify."""
        if user_input is not None:
            self._acct = self._username = cast(str, user_input[CONF_ACCOUNTS])
            self._password = self._accts[self._acct][CONF_PASSWORD]
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
            try:
                await self._verify_and_save_acct()
            except vol.EmailInvalid:
                errors[CONF_USERNAME] = "invalid_email"
            else:
                return await self.async_step_init()

        data_schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.EMAIL)
                ),
                vol.Required(CONF_PASSWORD): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
            }
        )
        if self._username:
            data_schema = self.add_suggested_values_to_schema(
                data_schema,
                {CONF_USERNAME: self._username, CONF_PASSWORD: self._password},
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
            return await self.async_step_init()

        return self.async_show_form(
            step_id="del_accts",
            data_schema=self._sel_accts_schema(multiple=True),
            last_step=False,
        )

    def _sel_accts_schema(self, multiple: bool) -> vol.Schema:
        """Create data schema to select one, or possibly more, account(s)."""
        # TODO: Include only enabled???
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
        # TODO: Actually check if login works!

        self._username = cast(str, vol.Email()(self._username))
        if self._acct:
            enabled = cast(bool, self._accts.pop(self._acct)[CONF_ENABLED])
        else:
            enabled = True
        self._accts[self._username] = {
            CONF_PASSWORD: self._password,
            CONF_ENABLED: enabled,
        }

    @abstractmethod
    async def async_step_done(self, _: dict[str, Any] | None = None) -> FlowResult:
        """Finish the flow."""


class Life360ConfigFlow(ConfigFlow, Life360Flow, domain=DOMAIN):
    """Life360 integration config flow."""

    VERSION = 2

    def __init__(self) -> None:
        """Initialize config flow."""
        self._options: dict[str, Any] = {CONF_ACCOUNTS: {}}

    @property
    def options(self) -> dict[str, Any]:
        """Return mutable copy of options."""
        return self._options

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> Life360OptionsFlow:
        """Get the options flow for this handler."""
        # Default first step is init.
        return Life360OptionsFlow(config_entry)

    async def async_step_user(self, _: dict[str, Any] | None = None) -> FlowResult:
        """Handle a config flow initiated by the user."""
        # manifest.json single_config_entry option added in 2024.3. Once versions before
        # that are no longer supported, this check can be removed.
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        return await self.async_step_init()

    async def async_step_done(self, _: dict[str, Any] | None = None) -> FlowResult:
        """Finish the flow."""
        return self.async_create_entry(title="Life360", data={}, options=self.options)


class Life360OptionsFlow(OptionsFlowWithConfigEntry, Life360Flow):
    """Life360 integration options flow."""

    async def async_step_done(self, _: dict[str, Any] | None = None) -> FlowResult:
        """Finish the flow."""
        return self.async_create_entry(title="", data=self.options)
