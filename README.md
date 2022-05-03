# ha-life360
Test project for converting Home Assistant Life360 integration to an entity-based device tracker

## Overview

Life360 was added to Home Assistant as a built-in integration right when the
[Device Tracker](https://www.home-assistant.io/integrations/device_tracker) component was being converted
from scanner based to entity based. The Life360 integration effectively became a "legacy" Device Tracker
platform before it was accepted.

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

option | description
-| -
`circles` `members` | `life360` entities can now be managed via the Entity Registry
`error_threshold` `warning_threshold` | The integration now uses better built-in error message management mechanisms
`max_update_wait` | `life360_update_overdue` & `life360_update_restored` events can no longer be generated. The `last_seen` attribute can be used instead to trigger automations.
`show_as_state: moving` | The `moving` attribute has been removed since it seems it was never really useful.

You may see a warning if you've been using any of these.

## Procedure

1. Install per above instructions.
2. Make a backup of your entire configuration, or at least the Life360 config entries as described above.
3. Restart Home Assistant.
4. The existing Life360 config entries will be migrated to the new version.
6. Since the previous entries still exist in `known_devices.yaml`, you'll see two entities for each Life360 member. E.g., `device_tracker.life360_me` and `device_tracer.life360_me_2`. The former is the legacy entity from `known_devices.yaml`, and is effectively useless now. The latter is the new, active entity.
7. Remove `known_devices.yaml`, or at least comment out the Life360 related entries.
8. Restart Home Assistant.
9. The legacy entities should be gone. Use the Entity Registry to remove the "`_2`" suffix from the Entity ID of each of the new Life360 entities.

## Restore procedure

1. Shut down Home Assistant.
2. Remove the `life360` directory from the `custom_components` directory in your Home Assistant configuration directory.
3. Restore from your configuration backup, or use the `life360_restore.py` script to restore the Life360 config entries and to remove the Life360 entities from the Entity Registry.
4. Restore `known_devices.yaml`.
5. Restart Home Assistant.

## PLEASE REMEMBER TO GIVE ME FEEDBACK & THANK YOU!
