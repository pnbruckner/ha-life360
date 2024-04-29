"""Constants for Life360 integration."""

from datetime import timedelta

from aiohttp import ClientTimeout

DOMAIN = "life360"

ATTRIBUTION = "Data provided by life360.com"
CIRCLE_UPDATE_INTERVAL = timedelta(hours=1)
COMM_MAX_RETRIES = 4
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

STATE_DRIVING = "driving"

CONF_ACCOUNTS = "accounts"
CONF_AUTHORIZATION = "authorization"
CONF_DRIVING_SPEED = "driving_speed"
CONF_MAX_GPS_ACCURACY = "max_gps_accuracy"
CONF_SHOW_DRIVING = "driving"
CONF_TOKEN_TYPE = "token_type"
CONF_VERBOSITY = "verbosity"

SIGNAL_ACCT_STATUS = "life360_acct_status"
