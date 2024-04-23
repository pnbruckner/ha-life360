"""Constants for Life360 integration."""

from datetime import timedelta
import logging

from aiohttp import ClientTimeout

DOMAIN = "life360"
LOGGER = logging.getLogger(__package__)

ATTRIBUTION = "Data provided by life360.com"
CIRCLE_UPDATE_INTERVAL = timedelta(hours=1)
COMM_MAX_RETRIES = 3
COMM_TIMEOUT = ClientTimeout(sock_connect=15, total=60)
SPEED_FACTOR_MPH = 2.25
SPEED_DIGITS = 1
UPDATE_INTERVAL = timedelta(seconds=5)

ATTR_ADDRESS = "address"
ATTR_AT_LOC_SINCE = "at_loc_since"
ATTR_DRIVING = "driving"
ATTR_LAST_SEEN = "last_seen"
ATTR_PLACE = "place"
ATTR_REASON = "reason"
ATTR_SPEED = "speed"
ATTR_WIFI_ON = "wifi_on"
ATTR_IGNORED_UPDATE_REASONS = "ignored_update_reasons"

# TODO: Add state to translations.
STATE_DRIVING = "Driving"

CONF_ACCOUNTS = "accounts"
CONF_AUTHORIZATION = "authorization"
CONF_CIRCLES = "circles"
CONF_DRIVING_SPEED = "driving_speed"
CONF_ERROR_THRESHOLD = "error_threshold"
CONF_MAX_GPS_ACCURACY = "max_gps_accuracy"
CONF_MAX_UPDATE_WAIT = "max_update_wait"
CONF_MEMBERS = "members"
CONF_SHOW_AS_STATE = "show_as_state"
CONF_SHOW_DRIVING = "driving"
CONF_VERBOSITY = "verbosity"
CONF_WARNING_THRESHOLD = "warning_threshold"

SHOW_MOVING = "moving"

DEFAULT_OPTIONS = {
    CONF_DRIVING_SPEED: None,
    CONF_MAX_GPS_ACCURACY: None,
    CONF_SHOW_DRIVING: False,
}
OPTIONS = list(DEFAULT_OPTIONS.keys())

DATA_CONFIG_OPTIONS = "config_options"
DATA_CENTRAL_COORDINATOR = "central_coordinator"
