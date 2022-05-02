# ha-life360
Test project for converting Home Assistant Life360 integration to an entity-based device tracker

## Overview

Life360 was added to Home Assistant as a built-in integration right when the
[Device Tracker](https://www.home-assistant.io/integrations/device_tracker) component was being converted
from scanner based to entity based. It effectively became a "legacy" Device Tracker platform before it was
accepted.

I've long wanted to convert it to the new entity-based structure. This is the result. But before I submit
it officially, and possibly affect many users with potential issues I didn't foresee, I wanted to get
feedback from users willing to give it a try. If you're willing, read on...

## Versions

This has been tested with Home Assistant 2022.4.7 using Python 3.9. If you're using different versions your
mileage may vary. If you'd still like to give it a try, let me know what versions you're using and I'll try
to test with them first.

## Backup

It should go without saying you should make a backup of your configuration before giving this a try. If
you don't have a good backup strategy I've written a couple of scripts that can save your current Life360
configuration entries ([`life360_backup.py`](custom_components/life360/life360_backup.py)) and restore them
later ([`life360_restore.py`](custom_components/life360/life360_restore.py)), e.g., when you remove this
custom integration. Running `life360_backup.py` will by default create a file named
`life360_config_backup.json` in the `.storage` sub-directory of your Home Assistant configuration directory.

## Installation

In theory this can be installed using HACS as an external repository. Or you can manually install it.
Basically you need to get all of the files & folders in [custom_components/life360](custom_components/life360)
into a similarly named folder in your Home Assistant configuration folder. If you've never done that and are
not sure how, feel free to ask me for help, either via the
[Home Assistant Forum](https://community.home-assistant.io/u/pnbruckner/summary) or by opening an
[issue here](https://github.com/pnbruckner/ha-life360/issues).

Once this custom integration is installed it will be used instead of the built-in integration.

## Options no longer supported

The following config options are no longer supported:

`circles` `error_threshold` `max_update_wait` `members` `show_as_state` `warning_threshold`

You may see a warning if you've been using any of these.

## Procedure

1. Install per above instructions.
2. Make a backup of your entire configuration, or at least the Life360 config entries as described above.
3. Restart Home Assistant.
4. The existing Life360 config entries will be migrated to a new version.
6. Since the previous entries still exist in `known_devices.yaml`, you'll see two entities for each Life360 member.
7. Remove `known_devices.yaml`, or at least comment out the Life360 related entries.
8. Restart Home Assistant.
9. Use the Entity Registry to remove the "`_2`" suffix from the Entity ID of each of the Life360 entities.

## Restore procedure

1. Shut down Home Assistant.
2. Remove the `life360` directory from the `custom_components` directory in your Home Assistant configuration directory.
3. Restore from your configuration backup, or use the `life360_restore.py` script to restore the Life360 config entries and to remove the Life360 entities from the Entity Registry.
4. Restore `known_devices.yaml`.
5. Restart Home Assistant.

## PLEASE REMEMBER TO GIVE ME FEEDBACK & THANK YOU!
