# ha-life360
Test project for significant changes to the Home Assistant [Life360](https://www.home-assistant.io/integrations/life360) integration

## Overview

This project provides a way for willing users to try out significant changes to the integration
before I submit them officially, and possibly affect many users with potential issues I didn't foresee.
It would be great to get feedback from real world usage. If you're willing, read on...

## Current Changes / Improvements

The main purpose of the current set of changes is to use a single, central "data coordinator" to retrieve
data from the Life360 server using all registered accounts (i.e., Life360 integrations), instead of each
account/integration retrieving data individually as is done in the current implementation.

As a bit of background, the last set of significant changes (introduced in 2022.7) converted the integration from
a "legacy" tracker to the new entity-based implementation.
At the same time the code made use of the "standard" DataUpdateCoordinator class. But that resulted in each Life360
account being retreived separately, and hence there was no easy way to coordinate the data from multiple accounts.

These new changes goes back to one, "central" coordinator that retrieves data for all accounts at the same time.
This allows the data to be handled in a much more intelligent way. E.g., once a Circle's Places & Members are
retrieved, they do not need to be retrieved again if another account can access it. Also, Members that are in
multiple Circles can have the best data used when they share their location differently in the different Circles.

Another significant change is that Members that have no available location data (e.g., they don't share location
with the Circles that are reachable, or their device has been off or has not had network access for a long time)
will still get registered in the Entity Registry and will have `device_tracker` entities created for them. This
keeps things consistent when Members "come and go" for these reasons. Of course, any entity can be hidden or
disabled via the Entity Registry if desired.

Lastly, a `binary_sensor` has been added for each account (aka config, aka integration) that indicates
if server communications are working ok - i.e., if it's "online". By default the name will be
"life360 online (USERNAME)", and the entity ID will be `binary_sensor.life360_online_username`, but they can,
of course, be changed via the Entities page, or the entity can be disabled if you'd rather not see it.

## Versions

This is being tested with Home Assistant 2021.12.10 & the latest available release, using Python 3.9. If you're using different versions your
mileage may vary. If you'd still like to give it a try, let me know what versions you're using and I'll try
to test with them first.

## Backup

It should go without saying you should make a backup of your configuration before giving this a try.

## Installation

In theory this can be installed using [HACS](https://hacs.xyz/) as a [custom repository](https://hacs.xyz/docs/faq/custom_repositories/).
Or you can manually install it.

Basically you need to get all of the files & folders in [custom_components/life360](custom_components/life360)
into a similarly named folder in your Home Assistant configuration folder. If you've never done that and are
not sure how, see some [suggestions below](#installation-suggestions), or feel free to ask me for help, either via the
[Home Assistant Forum](https://community.home-assistant.io/u/pnbruckner/summary) or by opening an
[issue here](https://github.com/pnbruckner/ha-life360/issues).

Once this custom integration is installed it will be used instead of the built-in integration.

## Attribute changes

attribute | changed to | description
-|-|-
`reason` | added | If a Member's location is not available, the entity's state will become `unknown` and this attribute will explain why.
`ignored_update_reasons` | added | If a Member's location update is temporarily ignored because its `last_seen` attribute has gone "backwards", or its `gps_accuracy` doesn't satisfy the specified limit, this attribute will indicate which apply.

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
