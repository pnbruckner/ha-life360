# ha-life360
Test project for converting Home Assistant Life360 integration to an entity-based device tracker

## Overview

[Life360](https://www.home-assistant.io/integrations/life360) was added to Home Assistant as a built-in integration right when the
[Device Tracker](https://www.home-assistant.io/integrations/device_tracker) component was being converted
from scanner based to entity based. The Life360 integration effectively became a "legacy" Device Tracker
platform before it was even accepted.

I've long wanted to convert it to the new entity-based structure. This is the result. But before I submit
it officially, and possibly affect many users with potential issues I didn't foresee, I wanted to get
feedback from users willing to give it a try. If you're willing, read on...

## Versions

This has been tested with Home Assistant 2022.4.7 & 2022.5.1, using Python 3.9. If you're using different versions your
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

In theory this can be installed using [HACS](https://hacs.xyz/) as an [custom repository](https://hacs.xyz/docs/faq/custom_repositories/).
Or you can manually install it.

Basically you need to get all of the files & folders in [custom_components/life360](custom_components/life360)
into a similarly named folder in your Home Assistant configuration folder. If you've never done that and are
not sure how, see some [suggestions below](#installation-suggestions), or feel free to ask me for help, either via the
[Home Assistant Forum](https://community.home-assistant.io/u/pnbruckner/summary) or by opening an
[issue here](https://github.com/pnbruckner/ha-life360/issues).

Once this custom integration is installed it will be used instead of the built-in integration.

## Procedures
### Install, prepare and run

1. Make a backup of your entire configuration, or at least the Life360 config entries as described above and the `known_devices.yaml` file.
2. Install per above instructions.
3. Remove `known_devices.yaml` if Life360 is the only legacy device tracker you are using, or at least comment out the Life360 related entries, or set their `track` items to `false`.
4. Restart Home Assistant.
5. The existing Life360 config entries will be migrated to the new version.

### Alternate: Install, run and reconfigure

This method would more closely match updating without realizing the integration has been converted to the new entity-based format.

1. Make a backup of your entire configuration, or at least the Life360 config entries as described above and the `known_devices.yaml` file.
2. Install per above instructions.
3. Restart Home Assistant.
4. The existing Life360 config entries will be migrated to the new version.
6. Since the previous entries still exist in `known_devices.yaml`, you'll see two entities for each Life360 member. E.g., `device_tracker.life360_me` and `device_tracer.life360_me_2`. The former is the legacy entity from `known_devices.yaml`, and is effectively useless now. The latter is the new, active entity.
7. Remove `known_devices.yaml` if Life360 is the only legacy device tracker you are using, or at least comment out the Life360 related entries, or set their `track` items to `false`.
8. Restart Home Assistant.
9. The legacy entities should be gone. Use the Entity Registry to remove the "`_2`" suffix from the Entity ID of each of the new Life360 entities.

### Restore

1. Shut down Home Assistant.
2. Remove the `life360` directory from the `custom_components` directory in your Home Assistant configuration directory.
3. Restore from your configuration backup, or use the `life360_restore.py` script to restore the Life360 config entries and to remove the Life360 entities from the Entity Registry.
4. Restore `known_devices.yaml`.
5. Restart Home Assistant.

## Attribute changes

attribute | changed to | description
-|-|-
`moving` | removed | The value from the server this was based on never seemed to be valid.
`raw_speed` | removed | This "raw" value was never really useful like the converted `speed` value.
`battery` | `battery_level` | This is a function of the Device Tracker component.

## Options no longer supported

The following config options are no longer supported:

option | description
-| -
`circles` `members` | `life360` entities can now be managed via the Entity Registry.
`error_threshold` `warning_threshold` | The integration now uses better built-in error message management mechanisms.
`max_update_wait` | `life360_update_overdue` & `life360_update_restored` events can no longer be generated. The `last_seen` attribute can be used instead to trigger automations.
`show_as_state: moving` | No longer meaninful since the `moving` attribute has been removed.

You may see a warning if you've been using any of these.

## Account options

You can change an account's options via the new `CONFIGURE` button when viewing the account on the Integrations page. These same options will be presented when creating a new account entry.

![Account Options](images/integration_options.png)

item | description
-|-
Use an object ID prefix | Check this box to use an [Entity Namespace](https://www.home-assistant.io/docs/configuration/platform_options/#entity-namespace)
Entity namespace | Prefix string for `device_tracker` object IDs
Limit GPS accuracy | Check this box to limit location updates based on location accuracy
Max GPS accuracy | If location's accuracy circle is larger than this value (i.e., _less_ accurate than this limit) the update will be ignored (always specified in meters)
Set driving speed threshold | Check this box to force `driving` attribute to be `True` if the `speed` attribute is at or above specified value
Driving Speed | Speed threshold (mph or kph, depending on Home Assistant Unit System selection)
Life360 server query period | Time between Life360 server queries (seconds)
Show driving as state | Check this box to change entity state to "Driving" when `driving` attribute is `True`

## PLEASE REMEMBER TO GIVE ME FEEDBACK & THANK YOU!

Which you can do either via the [Home Assistant Forum](https://community.home-assistant.io/t/life360-conversion-to-entity-based-device-tracker-testers-needed/422454)
or by opening an [issue here](https://github.com/pnbruckner/ha-life360/issues)

## Manual installation suggestions

### Download zip file from github

At the top of this page, click on the Code button and pick the "Download ZIP" option at the bottom.
This will download the entire project. Unzip it, and copy the `life360` foler into the `custom_components`
folder in your Home Assistant configuration directory.

### Use svn export

If you do not have subversion, you can install it using `sudo apt install subversion`.

`cd` into the `custom_components` folder in your Home Assistant configuration directory.
Enter the following command:

```
svn export https://github.com/pnbruckner/ha-life360/trunk/custom_components/life360
```

### Clone the project and add symolic link

This is actually the method I use. If you don't have git, you can install it using `sudo apt install git`.

First get whichever link you prefer by clicking on the Code button at the top of this page.
There should be three options: HTTPS, SSH & GitHub CLI. Click on whichever you like,
then click the copy button to the right of the link.

`cd` to some convenient directory, then enter the following command:

```
git clone <link_copied_from_code_button>
```
For example:
```
git clone https://github.com/pnbruckner/ha-life360.git
```
This will create a folder named `ha-life360`.

Now `cd` to `custom_components` in your Home Assistant configuration directory.
Enter the following command:

```
ln -s <path_to_ha-life360>/custom_components/life360 life360
```
