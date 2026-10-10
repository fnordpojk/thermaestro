# Setting up Thermaestro

This walks through a new installation: installing it, the first start, and the setup pages in the web UI.

## What it does today

Thermaestro reads. It connects to a Nibe pump, keeps every value with its quality and its history, and shows them with electricity prices and weather forecasts. It can publish all of it to Home Assistant over MQTT.

It doesn't change anything on the pump unless told to. The planner runs and decides what each setting should be, but every pump setting starts off. **Setup → Control** switches each one to shadow (decide and show what it would do, change nothing) or control; so does [the API](api.md#levers-and-the-plan). What the household wants goes on the **Intents** page.

If you want to help with a model Thermaestro doesn't know yet, [the read-only probe](probe.md) makes a report to send with an issue.

## Installing

### With Docker Compose

This is the way to run it. [docker.md](docker.md) has the steps: the compose file, the first build, updates, backups and moving an installation.

### From source

For development. It needs [uv](https://docs.astral.sh/uv/) and git; uv fetches Python 3.13 if the machine doesn't have it.

```
git clone https://github.com/fnordpojk/thermaestro.git
cd thermaestro
uv sync
```

Thermaestro keeps its files in `/etc/thermaestro` and `/var/lib/thermaestro` unless told otherwise. To keep them in your home directory instead:

```
export CONFIGURATION_DIRECTORY=~/thermaestro-config STATE_DIRECTORY=~/thermaestro-state
uv run thermaestro run
```

## Where things live

| | Docker | Raspberry Pi | |
|---|---|---|---|
| configuration | `/config` (`./config` beside `compose.yaml`) | `/etc/thermaestro` | the start-up file, `thermaestro.toml` |
| state | `/data` (the `thermaestro_data` volume) | `/var/lib/thermaestro` | everything else |

The configuration directory holds only the start-up file, which Thermaestro reads and never writes. Without it, everything has its default:

```toml
[web]
listen = "0.0.0.0"
port = 8080           # HTTP
https = true          # also HTTPS, with a certificate of its own
https_port = 8443

[paths]
state = "/var/lib/thermaestro"

[history]
raw_days = 14         # samples as they came
aggregate_days = 400  # then 15-minute minimum, mean, maximum and last
```

The state directory holds what is set in the web UI or learned:

- `thermaestro.db`, the database;
- `secrets.json`, the tokens, passwords and keys you enter;
- `tls/`, the HTTPS certificate and its key;
- `audit/`, the audit log of every change;
- `maps/`, register maps converted from your own exports;
- `setup-code`, while there is no administrator.

The state directory must belong to the user Thermaestro runs as, and be closed to others (mode 0700, or 0750). Thermaestro won't start otherwise. Moving an installation is a copy of this directory.

## The first start

### The first administrator

Until there is an administrator, each start makes a setup code and logs it:

```
No administrator yet. Create one in the web UI with the setup code …; it works once, for 60 minutes.
```

`thermaestro setup-code` on the host prints it again, or a new one (`docker compose exec thermaestro thermaestro setup-code` in Docker).

Open the web UI at `http://<the machine's address>:8080`. The **First administrator** page asks for the setup code, a user name, and a password of at least 15 characters. Saving creates the user in the Administrators group and logs you in.

A user can also be made on the host, without the web UI: `thermaestro admin create <name>` asks for the password. `thermaestro admin reset-password <name>` sets a new one, and enables the user if it was disabled.

### HTTPS

HTTPS is on port 8443, with a certificate the installation makes for itself. It names the host, `<host>.local` and `localhost`, but not the machine's LAN address. It lasts 825 days, and a start within its last 30 days makes a new one. No authority signed it, so the browser warns. Compare its SHA-256 fingerprint with the one under **System → Health**, or the one in the log at start.

Everyone logs in, also on the home network. Changes under some rights (users, secrets, plugins) ask for your password again if you last entered it more than 15 minutes ago.

## The setup pages

**Setup** in the menu has seven topics: House, Pump, External, Sensors, Prices, Weather and Control. They can be done in any order, but some need others first:

- weather forecasts need the location, under House;
- sensors from Home Assistant need Home Assistant, under External;
- MQTT sensors and Home Assistant discovery need the MQTT broker, under External.

A good order is House (location), Pump, External, Sensors, then Prices and Weather.

### House

- **Location:** latitude and longitude with a decimal point, such as 59.33, and the time zone. The time zone is preselected from the browser. The location is for forecasts and the sun's position.
- **The location's climate:** the annual mean temperature and how far the warmest month's mean is from the coldest's. Thermaestro estimates the cold water's temperature from them. With Open-Meteo added under Weather, **Get it from Open-Meteo's archive** fills it in; otherwise enter it yourself.
- **About the house:** what heats each climate system (radiators, fan coils, underfloor heating in boards or grooves, floor heating in a concrete slab, or radiators with either), how well the house is insulated, whether the water comes from a private well or the municipal supply, the public holidays' calendar (a country, and maybe a region, such as `SE` or `DE-BY`) and which day of the week a holiday counts as, and what to do after a missed hot-water deadline. Thermaestro starts from these until it has learned the house: a concrete slab answers far more slowly than radiators (about 12 hours to their 2), so it plans earlier for it and regulates it more gently; underfloor heating in boards answers in about 3. An answer given as plain underfloor heating before the two were told apart was taken as a slab, and is marked to be checked.
- **Rooms:** a name, the climate system that heats the room, and the room's own control (none, a simple thermostat, a smart thermostat, radiator valves, a zone controller, or unknown). A room's temperature is the mean of its sensors, or its reference sensor alone. With Home Assistant connected, its areas can be made into rooms in one go.

### Pump

**The pump's connection** takes the pump's address and the protocol to use. **Save and connect** starts reading.

| protocol | for | what it needs |
|---|---|---|
| **NibeGW (plain)** | an F-series pump, or another Nibe model on the same accessory bus, through a NibeGW gateway such as [esphome-nibe](https://github.com/elupus/esphome-nibe) | the gateway's address |
| **Thermaestro gateway (with a key)** | the same pumps, through a gateway that speaks the [Thermaestro gateway protocol](gateway-protocol.md): `thermaestro-gateway` from this repository, or the [esphome-nibe fork](https://github.com/fnordpojk/esphome-nibe) (branch `thermaestro-protocol`) | the gateway's address, and its pre-shared key: 64 hex digits |
| **Modbus TCP (an S-series pump)** | an S-series pump, which needs no gateway | the pump's own address, and its model chosen from the list |

The Thermaestro gateway protocol tells Thermaestro what happened to each request it sends, and it carries the bus's timing and the gateway's health. Plain NibeGW works too, with less information.

The other fields:

- **Read port** (9999), **Write port** (10000) and **Control port** (10090): the gateway's ports, if it uses others. The control port is the Thermaestro gateway protocol's.
- **Modbus port** (502): an S-series pump's, if it answers on another one.
- **Local port** (0): where Thermaestro listens for the gateway. 0 picks a free one. esphome-nibe and the Thermaestro gateway answer where Thermaestro sends from, so they need nothing here. A gateway that always sends to a fixed port, such as openHAB's NibeGW, needs that port here.
- **Model:** a bus pump says its own model every 15 seconds, so leave it at "as the pump reports it" unless the name isn't recognized. An S-series pump's model must be chosen.
- **Brine, for the heat taken from the ground:** the pump doesn't measure the brine flow. With the flow from the installation's papers, the brine pump's speed it was measured at, and the brine mix, Thermaestro estimates the heat taken from the ground. Leave the flow empty for no estimate.

On an S-series pump, turn Modbus TCP on in the pump's menu 7.5.9. The pump answers only addresses on the local network. What Thermaestro reads from an S-series pump comes from Nibe's documents, and hasn't been checked on a real one yet.

**LOG.SET:** an F-series pump sends up to 20 registers listed in a LOG.SET file by itself, twice a second, so Thermaestro needn't ask for them one at a time. Download the file for your model, copy it to a USB stick and load it in the pump's USB menu (7.2). Pumps are particular about sticks: those larger than 4 GB, or made in about the last 15 years, often aren't accepted. Everything works without it, more slowly. `thermaestro nibe-logset F1245 LOG.SET` writes the same file on the host.

The pump's values are then on the **Pump** page: the everyday ones, the settings read from the pump, and diagnostic values.

### External

- **Home Assistant:** its address, such as `http://homeassistant.local:8123`, and a long-lived access token, made in Home Assistant under your profile, Security. Thermaestro reads the entities you choose through Home Assistant's WebSocket API. **Save and choose entities** goes on to the list. In Docker, addresses ending in `.local` don't resolve; use an IP address or a name your DNS knows.
- **MQTT broker:** address, port (1883), user name, password, and TLS.
- **Home Assistant discovery:** with **Publish to Home Assistant** ticked, Thermaestro and the pump appear in Home Assistant as devices, through the MQTT broker. They carry the rooms' and the outdoor values, the electricity price, the pump's values, and how the plugins are doing. The pump's settings and diagnostic values come disabled; enable those you want in Home Assistant. Nothing is accepted back. The options:
  - **Language of the names:** English, Swedish or German;
  - **Also each sensor's own values**, which Home Assistant may have already;
  - **Discovery prefix** (`homeassistant`), as set in Home Assistant's MQTT integration;
  - **Topic prefix** (`thermaestro`), where the values go.

### Sensors

Sensors from outside the pump, each placed in a room, outdoors, or elsewhere.

- **From Home Assistant:** tick the entities to read, then give each a name and a room. Each picked value becomes a sensor.
- **Add an MQTT sensor:** a name, the topic (such as `zigbee2mqtt/bedroom`), and the JSON field holding the value; leave the field empty if the payload is the number itself.

Each sensor also has:

- **Measures:** what it measures;
- **Where:** in a room, outdoors, or elsewhere, and which room;
- **the room's reference:** this sensor alone gives the room's temperature;
- **Calibration:** added to each value;
- **Stale after (minutes):** when a value stops counting. Leave it empty and Thermaestro learns it from how often the sensor reports, between 60 and 720 minutes.

**Outdoor references** picks the outdoor sensor that counts for each quantity: forecasts are checked against it. Only sensors placed outdoors are offered. Without one for temperature, the pump's own outdoor sensor is used.

### Prices

What one more kWh costs is a stack of layers: the spot price, the supplier's adders, taxes and grid fees, and VAT. Thermaestro adds them per 15 minutes, and refuses a stack that counts something twice.

1. **Spot price:** choose your bidding zone and **Show its sources**. Thermaestro suggests a source for the zone and a fallback for a day the first has none. Every zone of a country takes its prices from the same source. Saving sets up the source and adds the spot layer to the stack.

   | where | suggested source |
   |---|---|
   | Sweden | Beneficial Apps' Nordic price site, elprisetjustnu.se (15 minutes) |
   | Norway, Denmark, Finland | Energy-Charts, with Beneficial Apps' Nordic price site (hourly) as the fallback |
   | Spain, Portugal | OMIE |
   | other countries | Energy-Charts; in a country where some of its zones are for private use only and you have entered an ENTSO-E token, ENTSO-E first |

   Where Energy-Charts' prices for a zone are for private and internal use only, the page says so. Tibber, with your token, comes first in the countries it sells in; ENTSO-E, with your token, is offered for every zone.
2. **Tibber:** a personal access token from developer.tibber.com. It gives your home's prices per 15 minutes, the spot price and what Tibber charges with VAT. Thermaestro asks Tibber for prices only, never for your name, address or consumption.
3. **ENTSO-E:** the bidding zone, the currency, and a security token of your own. Register on the ENTSO-E Transparency Platform, then ask its helpdesk for access to the API. Prices in euros are converted at the ECB's reference rates.
4. **Octopus Agile:** for a household in Great Britain on Octopus Energy's Agile tariff, choose the region, then add its unit rate as a layer.
   **Denmark:** **Show the grid companies** lists every grid company's household tariff from Energi Data Service (Energinet's open data, no account); pick yours. Thermaestro then reads its tariff hour by hour (with any rebate on it), Energinet's transmission and system tariffs, and the electricity tax, and adds each as a layer where no layer has its role yet. Nothing about the house is sent.
5. **Grid rules:** your grid company's rules, entered by hand from its price list.
   - **A time-of-use price:** a base price per kWh, and the prices that apply instead at some times (months, working days or weekends, hours). The first that holds applies. It becomes a layer of the stack by itself.
   - **A power charge:** when power counts, how it is measured (per 15 minutes or per hour), how many of the month's highest intervals are averaged, whether on different days, and the price per kW. While it is in force, in its window, the planner keeps the house under the month's highest intervals so far, so a new one doesn't raise the bill. That needs the house's power from a meter (a `grid.import.power` sensor, in W or kW), and a rule that says how it is measured and how many intervals count.
   - **A subscribed power:** the most the house may draw; the planner keeps under it.
   - Leave a field empty when the grid company doesn't say: the rule lists it as not given. Working days leave out public holidays, from the calendar chosen under House. A rule's clock is civil time, or normal time all year where the grid company measures so. A rule that is paused, announced or withdrawn is kept and charges nothing.
6. **Layers:** add the series the sources offer, or fixed amounts per kWh for the supplier's charge, energy tax, a flat grid fee, levies or subsidies, each with or without VAT.
7. **VAT:** the rate in percent, and the layers it is charged on.

The **Prices** page shows the result.

### Weather

Forecasts for the location, which must be set first, under House.

**Add a provider:**

- **MET Norway:** the forecast behind Yr, for the whole world. No sunlight.
- **SMHI:** Sweden's forecast, for the Nordic and Baltic countries only. Thermaestro derives the dew point and sunlight.
- **Open-Meteo:** many national and global models through one service, sunlight included, with a model to choose. Free for non-commercial use.
- **Home Assistant:** a weather entity's hourly forecast, to stand in while the chosen provider has none. Offered once Home Assistant is connected.

Several can run side by side. **Which provider for what** sets the main provider, a fallback, and another provider for any quantity. **What each provider gives** lists each one's quantities, steps and reach, and its terms. After some days, the **Weather** page shows how each has done at this house.

### Control

Each setting Thermaestro could change, by part of the pump: the heating offset of each climate system, the hot-water mode, holding hot-water charges off, one extra charge, the addition's stop temperature and power, and the pool's where there is one. Each is:

- **Off** (the start): Thermaestro reads and plans, and changes nothing.
- **Shadow:** it decides as it would in control, shows what it would have done at the bottom of the page, and changes nothing. **Shadow vs the pump** (linked there and from the Plan page) sets each decision beside what the pump showed at that moment, to compare with what the pump did meanwhile, by itself or as another program such as NibePi set it: a chart per setting over the last day, week or 30 days (the pump's line and shadow's steps; a hold or a start as bands), the decisions in a table by setting and day, and a CSV download of the same.
- **Control:** it makes the change, reads it back, limits how often it changes, and puts it back as it found it when Thermaestro stops, when the room sensor a setting rests on goes quiet, or when the planner stops answering.

Switching needs the password again. A setting the pump has its own feature for (its hot-water schedule, Smart Price Adaption) asks you to confirm the feature is off first: two controllers on one setting fight. The heating offset also needs the pump's own room control off, which Thermaestro reads from the pump itself. A setting someone changed elsewhere (the pump's menu, NibePi) is let go and never written over; **Keep the change** takes it over again from there.

On a plain NibeGW gateway, the page also says that the gateway's write port lets anyone on the network change the pump's settings. The Thermaestro gateway protocol, with its key, closes that.

The page shows the day's changes against the planner's budget (about 50 a day).

## What the household wants: the Intents page

**Intents** in the menu says what applies now, lists what has been asked for, with when each ends and who asked, and takes new requests. Each one asked for folds out to show what it holds: its bands, temperatures and times, the levels it picks, how much it matters, its state and when it was asked. **Change** under it opens a form filled in with what it holds; **Save** changes it in place, checked as a new request would be. Whoever may end an intent may change it. What Thermaestro first took from how the pump runs is marked; **Keep it** makes it the household's own, as changing it does.

- **For a while** (also on the overview): warmer or cooler, until the pattern next changes, at most a day; a bath by a time; one extra charge of hot water now; a fireplace; away until a time, with a level for each climate system; guests; hands off for up to 48 hours. Each ends by itself, and **End** ends it sooner.
  - It shows once there is something to control: a climate system or a hot-water tank. After a restart, that is once the pump has been read.
  - The usual ones are buttons: **Warmer**, **Cooler**, **Extra hot water**, **Away** and **Fireplace**.
  - Anything else can be said in words, in English, Swedish or German: "a bath at 19:30", "borta till söndag", "Gäste bis morgen 18 Uhr", "lite varmare till 22", "hands off for 2 days". Thermaestro reads it with rules of its own (nothing is sent anywhere) and answers with what it understood, as a form to check and change; nothing is asked until **Ask for it**. What it can't tell, such as when you're back, is left for you to fill in, and what it doesn't understand is answered with examples.
- **Always:** comfort, as a band on a weekly pattern of levels (or, for a climate system without a room sensor, how far price may move its heat); hot water ready by times, and the lowest it may get; cost and comfort, the savings slider and what gives way first; the addition; the pool; a power limit; quiet hours.
- **Levels:** the household's own named bands and hot-water tops. **Change** under each one renames or moves it; every intent that names it follows. A level used by an intent can't be removed.

Asking answers at once: until when it applies, what it sets aside, and what the house can't do ("no room sensor: normal heat, shifted by price within a bound instead of a band"). The household's group may ask for things for a while; changing what always applies needs more rights ([the rights](api.md#rights)).

## What Thermaestro decides: the Plan page

**Plan** in the menu shows what the planner decided in its last round, for each setting: what it asked, why (in household terms, with what it serves: the hot water's floor, a ranked goal, or price), and what became of it. Below are the hot-water deadlines ahead with the cheapest time to charge for each, the house's power against its limit (the household's, or the grid company's where lower, with why), what shadow would have done, and every change asked in the last day. The planner decides for now from what applies now, every 15 minutes; a plan that looks a day or two ahead comes with the house's model.

## Users and groups

**System → Users** has three groups to start with:

- **Administrators:** everything;
- **Household:** see the values, and ask for things for a while (warmer, a bath, away, guests) once that is built;
- **Viewers:** see the values.

Groups can be changed and added, with rights named after what they allow. The page lists them.

Each user's own page, under their name in the menu, has the language and formats, where they are logged in, and API tokens. A token carries only the rights given to it, never more than its user's. With one, `thermaestro status` prints what Thermaestro reads:

```
THERMAESTRO_TOKEN=<token> thermaestro status --url http://<address>:8080
```

The pages that show how things stand (the overview, the pump, prices, weather, intents, the plan and System → Health) keep themselves current: every minute while they are in view, and at once when you come back to them. A form being filled in isn't touched. A page left open still logs out after 12 hours unused.

**Wall displays.** For a screen that shows Thermaestro all the time, such as a tablet on the wall, the account page makes a display link (with the right `wall_displays.own`, which Administrators have). Give it a name and the rights it needs, usually just seeing the values and the plan. Open the link in that screen's browser within a day; it works once, and that browser then stays logged in, without a password typed on it, until you revoke the display on the account page or change your password. A display can't make changes that need a password.

## Checking on it

- **System → Health:** each plugin's state, restarts and health counters, and the HTTPS certificate's fingerprint.
- **System → Audit log:** every change, and who made it. `thermaestro audit verify` checks the log's chain on the host.
- The overview lists what needs attention.
