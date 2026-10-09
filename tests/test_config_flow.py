"""Exercise account setup without contacting Life360."""

from unittest.mock import MagicMock, patch

from life360 import CommError, Life360Error, LoginError, RateLimited, Unauthorized
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.life360.const import DOMAIN


async def token_form(hass: HomeAssistant) -> dict:
    """Open the access-token form through the real configuration flow."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"driving": False, "verbosity": "0"}
    )
    assert result["step_id"] == "acct_type_menu"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "acct_authorization"}
    )


TOKEN_INPUT = {
    "username": "Parent",
    "authorization": "test-token",
    "token_type": "Bearer",
    "enabled": True,
}


async def test_valid_token_is_checked_before_acceptance(
    hass: HomeAssistant, MockLife360: MagicMock
) -> None:
    """Accept a token only after a successful authenticated read."""
    form = await token_form(hass)
    result = await hass.config_entries.flow.async_configure(
        form["flow_id"], TOKEN_INPUT
    )

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "acct_menu"
    api = MockLife360.apis[-1]
    api.get_me.assert_awaited_once_with()
    api.login_by_username.assert_not_awaited()
    assert api.authorization == "Bearer test-token"

    with patch("custom_components.life360.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "done"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].options["accounts"]["Parent"]["authorization"] == (
        "Bearer test-token"
    )


@pytest.mark.parametrize(
    ("MockLife360", "expected_error"),
    [
        ({None: {"get_me": [LoginError("expired token")]}}, "invalid_auth"),
        ({None: {"get_me": [Unauthorized("expired token", None)]}}, "invalid_auth"),
        ({None: {"get_me": [CommError("unreachable", None)]}}, "cannot_connect"),
        (
            {None: {"get_me": [RateLimited("slow down", retry_after=120)]}},
            "rate_limited",
        ),
        ({None: {"get_me": [Life360Error("unexpected reply")]}}, "unknown"),
    ],
    indirect=["MockLife360"],
)
async def test_failed_token_check_keeps_the_form_open(
    hass: HomeAssistant, MockLife360: MagicMock, expected_error: str
) -> None:
    """Reject failed authentication and distinguish service failures."""
    form = await token_form(hass)
    result = await hass.config_entries.flow.async_configure(
        form["flow_id"], TOKEN_INPUT
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "acct_authorization"
    assert result["errors"] == {"base": expected_error}
    assert not hass.config_entries.async_entries(DOMAIN)
    MockLife360.apis[-1].get_me.assert_awaited_once_with()


async def test_disabled_account_skips_token_validation(
    hass: HomeAssistant, MockLife360: MagicMock
) -> None:
    """Allow a disabled connection to be saved without contacting the server."""
    form = await token_form(hass)
    result = await hass.config_entries.flow.async_configure(
        form["flow_id"], TOKEN_INPUT | {"enabled": False}
    )

    assert result["step_id"] == "acct_menu"
    MockLife360.assert_not_called()


async def test_token_input_is_masked(hass: HomeAssistant) -> None:
    """Use Home Assistant's password selector for the session secret."""
    form = await token_form(hass)
    selector = next(
        selector
        for key, selector in form["data_schema"].schema.items()
        if key.schema == "authorization"
    )
    assert selector.config["type"] == "password"


@pytest.mark.parametrize(
    "MockLife360",
    [{None: {"login_by_username": [RateLimited("slow down", retry_after=60)]}}],
    indirect=True,
)
async def test_password_login_reports_rate_limit(
    hass: HomeAssistant, MockLife360: MagicMock
) -> None:
    """Report a rate limit during password login without saving the account."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"driving": False, "verbosity": "0"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "acct_username_password"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"username": "parent@example.com", "password": "test-password", "enabled": True},
    )

    assert result["step_id"] == "acct_username_password"
    assert result["errors"] == {"base": "rate_limited"}
    MockLife360.apis[-1].login_by_username.assert_awaited_once()


async def options_token_form(hass: HomeAssistant) -> tuple[MockConfigEntry, dict]:
    """Open the token form for an existing account."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        options={
            "accounts": {
                "Parent": {
                    "authorization": "Bearer saved-token",
                    "password": None,
                    "enabled": True,
                }
            },
            "driving": False,
            "driving_speed": None,
            "max_gps_accuracy": None,
            "verbosity": 0,
        },
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"driving": False, "verbosity": "0"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "mod_acct_sel"}
    )
    assert result["step_id"] == "acct_authorization"
    return entry, result


async def test_options_flow_checks_and_saves_replacement_token(
    hass: HomeAssistant, MockLife360: MagicMock
) -> None:
    """Validate a replacement token before updating the existing connection."""
    entry, form = await options_token_form(hass)
    result = await hass.config_entries.options.async_configure(
        form["flow_id"], TOKEN_INPUT
    )
    assert result["step_id"] == "acct_menu"
    MockLife360.apis[-1].get_me.assert_awaited_once_with()
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "done"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["accounts"]["Parent"]["authorization"] == "Bearer test-token"


@pytest.mark.parametrize(
    ("MockLife360", "expected_error"),
    [
        ({None: {"get_me": [Unauthorized("expired token", None)]}}, "invalid_auth"),
        ({None: {"get_me": [CommError("unreachable", None)]}}, "cannot_connect"),
        (
            {None: {"get_me": [RateLimited("slow down", retry_after=120)]}},
            "rate_limited",
        ),
    ],
    indirect=["MockLife360"],
)
async def test_options_flow_preserves_account_when_token_check_fails(
    hass: HomeAssistant, MockLife360: MagicMock, expected_error: str
) -> None:
    """A rejected replacement leaves the saved credentials unchanged."""
    entry, form = await options_token_form(hass)
    result = await hass.config_entries.options.async_configure(
        form["flow_id"], TOKEN_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected_error}
    assert entry.options["accounts"]["Parent"]["authorization"] == "Bearer saved-token"
    MockLife360.apis[-1].get_me.assert_awaited_once_with()
