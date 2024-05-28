# <img src="https://brands.home-assistant.io/life360/icon.png" alt="Life360" width="50" height="50"/> Life360

A [Home Assistant](https://www.home-assistant.io/) integration for Life360.
Creates Device Tracker (`device_tracker`) entities to show where Life360 Members are located.

## Current Changes / Improvements

As of HA 2024.2 the built-in Life360 integration was removed due to the integration effectively being broken and seemingly unrepairable.
It appeared Life360 and/or Cloudflare were actively blocking third party usage of their API.
However, since that time, a better understanding of the (undocumented & unsupported) API has been developed.
This custom integration is now able to use the API again.
It's, of course, yet to be seen if it will continue to work.

### Note on Updating Circles & Members Lists

The current implementation differs from previous versions in the way it retrieves the list of Circles visible to the registered accounts
as well as the list of Members in each of those Circles.
This is due to the fact that the server seems to severly limit when the list of Circles can be retrieved.
It is not uncommon for the server to respond to a request for Circles with an HTTP error 429, too many requests,
or an HTTP error 403, forbidden (aka a login error.)
When this happens the request must be retried after a delay of as much as ten minutes.
It may even need to be retried multiple times before it succeeds.

Therefore, when the integration is loaded (e.g., when the integration is first added, when it is reloaded, or when HA starts)
a WARNING message may be issued stating that the list of Circles & Members could not be retrieved and needs to be retried.
Once the lists of Circles & Members is retrieved successfully, there will be another WARNING message saying so.

Device tracker entities cannot be created until the lists of Circles & Members is known.

Once this process has completed the first time, the lists will be saved in storage (i.e., config/.storage/life360).
When the integration is reloaded or HA is restarted, this stored list will be used so that the tracker entities
can be created and updated normally.
At the same time, the integration will try to update the lists again from the server, so WARNING messages may be seen again.

Due to the above, new Circles or Members will only be seen (and corresponding tracker entities created) when the integration is loaded.
Therefore, if the registered accounts are added to any new Circles, or any Members are added to the known Circles,
the integration will not be aware of those changes until it is loaded.
This will happen at the next restart, or you can force it to happen by reloading the integration.
I.e., go to Settings -> Devices & services -> Life360,
click on the three dots next to "CONFIGURE" and select Reload.
Please be patient since it could take a while due the above reasons before any new tracker entities are created.

## Installation

The integration software must first be installed as a custom component.

You can use HACS to manage the installation and provide update notifications:

<details>
<summary>With HACS</summary>

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)

1. Add this repo as a [custom repository](https://hacs.xyz/docs/faq/custom_repositories/).
   It should then appear as a new integration. Click on it. If necessary, search for "life360".

   ```text
   https://github.com/pnbruckner/ha-life360
   ```
   Or use this button:
   
   [![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=pnbruckner&repository=ha-life360&category=integration)


1. Download the integration using the appropriate button.

</details>

Or you can manually install the software:

<details>
<summary>Manual Installation</summary>

Place a copy of the files from [`custom_components/life360`](custom_components/life360)
in `<config>/custom_components/life360`,
where `<config>` is your Home Assistant configuration directory.

>__NOTE__: When downloading, make sure to use the `Raw` button from each file's page.

</details>

>__NOTE__: After it has been downloaded you will need to restart Home Assistant.

## Configuration
### Add Integration Entry

After installation a Life360 integration entry must be added to Home Assistant.
This only needs to be done once.

Use this My Button:

[![add integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start?domain=life360)

Alternatively, go to Settings -> Devices & services and click the **`+ ADD INTEGRATION`** button.
Find or search for "Life360", click on it, then follow the prompts.

### Configuration Options
#### Max GPS Accuracy

Each location update has a GPS accuracy value (see the entity's corresponding attribute.)
You can think of each update as a circle whose center is defined by latitude & longitude,
and whose radius is defined by the accuracy value,
where the actual location of the device is somewhere within that circle.
The _higher_ the accuracy value, the _larger_ the circle where the device may be,
therefore the _less_ accurate the location fix.

This configuration option can be used to reject location updates that are _less_ accurate
(i.e., have _larger_ accuracy values) than the entered value (in meters.)

#### Driving Speed Threshold

The Life360 server indicates when it considers the device is moving at driving speeds.
However, this value does not always seem to be as expected.
This value can be overridden by providing a speed, at which or above, the entity's `driving` attribute should be true.

#### Show Driving as State

If enabled, and the device is determined to be at or above driving speed,
the state of the entity will be set to "Driving", assuming it is not within a Home Assistant Zone.

#### DEBUG Message Verbosity

If the user's profile has "advanced mode" enabled, then this configuration option will appear.
It can be used to adjust how much debug information should be written to the system log,
assuming debug has been enabled for the Life360 integration.

### Life360 Accounts

At least one Life360 account must be entered, although more may be entered if desired.
The integration will look for Life360 Members in all the Circles that can be seen by the entered account(s).

#### Account Authorization Methods

There are currently two methods supported for authorizing the Life360 integration to rerieve data associated with a Life360 account.

##### Username & Password

This method can be used with any Life360 account that has not had a phone number "verified."
Once a phone number has been verified, the Life360 server will no longer allow this authorization method.

Enter the Life360 account's email address & password.

##### Access Type & Token

This method is effectively a work around for accounts that have had a phone number "verified."
In theory, there is a way to "login" to the Life360 server using a phone number and a code sent via SMS.
However, I have not been able to get that to work.

Go to https://life360.com/login.
Open the browser's Developer Tools sidebar & go to the Network tab.
Make sure recording is enabled.
Log into Life360.
When the process has been completed look for the "token" packet.
(If there is one labeled "preflight", uses the OPTIONS method, or has no preview/response data,
ignore it and look for another "token" packet which uses the POST method and has data.)
Under the Preview or Response tab, look for `token_type` & `access_token`.
Copy those values into the corresponding boxes (access type & access token) on the HA account page.
(Note that the `token_type` is almost certainly "Bearer".)
You can put whatever you want in the "Account identifier" box.

## Versions

Home Assistant 2023.8 or newer is currently supported.

## Services

### `life360.update_location`

Can be used to request a location update for one or more Members.
Once this service is called, the Member's location will typically be updated every five seconds for about one minute.
The service takes one parameters, `entity_id`, which can be a single entity ID, a list of entity ID's, or the word "all" (which means all Life360 trackers.)
The use of the `target` parameter should also work.
