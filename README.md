# ha-life360
Test project for significant changes to the Home Assistant [Life360](https://www.home-assistant.io/integrations/life360) integration

## Overview

This project provides a way for willing users to try out significant changes to the integration
before I submit them officially, and possibly affect many users with potential issues I didn't foresee.
It would be great to get feedback from real world usage. If you're willing, read on...

## Current Changes / Improvements

As of HA 2024.2 the built-in Life360 integration was removed due to the integration effectively being broken and seemingly unrepairable.
It appeared Life360 and/or Cloudflare were actively blocking third party usage of their API.
However, since that time, a better understanding of the (undocumented & unsupported) API has been developed.
This custom integration is now able to use the API again.
It's, of course, yet to be seen if it will continue to work.

## Versions

Home Assistant 2023.8 or newer is currently supported.

## Installation

In theory this can be installed using [HACS](https://hacs.xyz/) as a [custom repository](https://hacs.xyz/docs/faq/custom_repositories/).
Or you can manually install it.

Basically you need to get all of the files & folders in [custom_components/life360](custom_components/life360)
into a similarly named folder in your Home Assistant configuration folder. If you've never done that and are
not sure how, see some [suggestions below](#installation-suggestions), or feel free to ask me for help, either via the
[Home Assistant Forum](https://community.home-assistant.io/u/pnbruckner/summary) or by opening an
[issue here](https://github.com/pnbruckner/ha-life360/issues).

Once this custom integration is installed it will be used instead of the built-in integration (which, of course, does not exist at this time.)

## Services

A new service, `life360.update_location`, can be used to request a location update for one or more Members.
Once this service is called, the Member's location will typically be updated every five seconds for about one minute.
The service takes one parameters, `entity_id`, which can be a single entity ID, a list of entity ID's, or the word "all" (which means all Life360 trackers.)
The use of the `target` parameter should also work.

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
