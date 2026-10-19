# Setting up Thermaestro

This walks through a new installation: installing it, the first start, and the setup pages in the web UI.

## What it does today

Thermaestro reads. It connects to a Nibe pump, keeps every value with its quality and its history, and shows them with electricity prices and weather forecasts. It can publish all of it to Home Assistant over MQTT.

It doesn't change anything on the pump unless told to. Two planners run and decide what each setting should be, the rules and [the optimizer](optimizer.md), but every pump setting starts off. **Setup → Control** switches each one to shadow (decide and show what it would do, change nothing) or control; so does [the API](api.md#levers-and-the-plan). What the household wants goes on the **Intents** page.

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

**Setup** in the menu has nine topics: House, Pump, External, Sensors, Prices, Weather, Control, Learned and From NibePi. Coming from NibePi, start with the last: it fills in much of the rest from NibePi's settings (see [Coming from NibePi](nibepi.md)). The topics can be done in any order, but some need others first:

- weather forecasts need the location, under House;
- sensors from Home Assistant need Home Assistant, under External;
- MQTT sensors and Home Assistant discovery need the MQTT broker, under External.

A good order is House (location), Pump, External, Sensors, then Prices and Weather.

### House

- **Location:** latitude and longitude with a decimal point, such as 59.33, and the time zone. The time zone is preselected from the browser. The location is for forecasts and the sun's position.
- **The location's climate:** the annual mean temperature and how far the warmest month's mean is from the coldest's. Thermaestro estimates the cold water's temperature from them. With Open-Meteo added under Weather, **Get it from Open-Meteo's archive** fills it in; otherwise enter it yourself.
- **About the house:** what heats each climate system (radiators, fan coils, underfloor heating in boards or grooves, floor heating in a concrete slab, or radiators with either), how well the house is insulated, whether the water comes from a private well or the municipal supply, the public holidays' calendar (a country, and maybe a region, such as `SE` or `DE-BY`) and which day of the week a holiday counts as, and what to do after a missed hot-water deadline. Thermaestro starts from these until it has learned the house: a concrete slab answers far more slowly than radiators (about 12 hours to their 2), so it plans earlier for it and regulates it more gently; underfloor heating in boards answers in about 3. An answer given as plain underfloor heating before the two were told apart was taken as a slab, and is marked to be checked.
- **From the documentation:** each hot-water tank's volume and each compressor's COP, which what heat costs is reckoned from. A pump's plugin gives them for the models it knows (Nibe's F1245: its 178 l tank, COP 4.47 at 0/35 °C and 3.51 at 0/45 for the 6 kW model, 2.8 making hot water), shown with where they come from. Otherwise enter them from the pump's documentation: the tank's liters, and the COP heating with the brine (or outdoor air) and supply temperatures it is stated for, usually 0 and 35 °C.
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
- **An electricity meter on a heat pump's supply**, where there is one (most pumps have none): an MQTT topic or a Home Assistant entity measuring energy, with "On the supply of" set to the pump. A meter that counts pulses gives its counts per kWh (a raw count of 10000 per kWh: `10000`); an entity in kWh, or in Wh as Home Assistant says, needs none. A counter is kept at most every 10 seconds, and one that starts over from 0 is read as such. Once it has counted faster while the compressor runs than while it stands, the compressor's draw and each hot-water charge's electricity are read from it (see Learned).

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
- **Control:** it makes the change, reads it back, limits how often it changes, and puts it back when Thermaestro stops, when the room sensor a setting rests on goes quiet, or when the planner stops answering.

Switching needs the password again. A setting the pump has its own feature for (its hot-water schedule, Smart Price Adaption) asks you to confirm the feature is off first: two controllers on one setting fight. The heating offset also needs the pump's own room control off, which Thermaestro reads from the pump itself. A setting someone changed elsewhere (the pump's menu, NibePi) is let go and never written over; **Keep the change** takes it over again from there.

**Put back to** is what a setting is put back to: at first what Thermaestro found it at, or a value you choose, such as your usual heating offset rather than whatever another program last left it at. It needs the password again too.

On a plain NibeGW gateway, the page also says that the gateway's write port lets anyone on the network change the pump's settings. The Thermaestro gateway protocol, with its key, closes that.

The page shows the day's changes against the planner's budget (about 50 a day).

**Which planner drives:** the rules, or [the optimizer](optimizer.md), which plans the next two days at once from what Thermaestro has learned. Both decide every slot; the one chosen drives, and the other's decisions are kept beside it on the Plan page. The optimizer drives only the goals whose learned models have proved themselves, and the rules the rest; the page lists what it covers and why the rest is left to the rules. Switching needs the password again, and Thermaestro never switches by itself.

### Learned

What Thermaestro has learned, and what it learns from. Learning only watches: it never changes anything.

**What it learns from** lists each value learning would use, and whether it can: a value a pump offers isn't always a measurement, since some answer 0, or the same value forever. Each one must first have behaved as its quantity would over the last week:

- recorded for at least a day, with good values, and changing where its quantity changes;
- in step with the values it goes with: the compressor runs only while there is a demand, the tank warms during hot-water charges, the supply is warmer while the compressor heats the house, and the house's power is higher while the compressor runs.

Each is **Usable**, **Not usable on this pump** (with what was seen, such as "Always 0 in the last week"), or **Not seen enough yet** (no hot-water charge in the week, the addition never running). One that isn't usable is left out. A week that shows nothing doesn't undo an earlier proof: the addition may run only for the periodic hot-water increase and on the coldest days, so it stays usable, with when it was last seen. The check runs every hour, ten minutes after a start the first time, and **Check now** runs it at once.

**Hot water, the last week** counts the draws and charges read for each hot-water tank. Once a day is over, it is read from the tank's two temperatures and the pump's demand:

- a **charge** is the demand on hot water for ten minutes or more; during one, and for 15 minutes after, the charge sensor shows the charge rather than the tank, so it isn't read;
- the **standing loss** is how fast the tank cools by itself, from its slowest hours outside charges;
- a **draw** is a fall faster than that, by more than the tank's own quiet noise. A draw that makes the pump start a charge counts up to the charge. Its size is in degrees of the tank: how far it takes the mean of the two temperatures down;
- the **week**: what is drawn in each hour of the week, learned day by day. A public holiday counts as the weekday chosen under House. Where the location's climate is known (Weather), a draw is compared across seasons at the year's mean cold-water temperature, from the water source answered under House.

Days already recorded are read at once, as far back as the raw history goes (two weeks unless set otherwise). A day whose readings stop for more than an hour, Thermaestro or the pump's connection down, is passed over rather than learned as a day with nothing drawn; the compressor's days likewise. The tank is learned only once its top, its charge sensor and the pump's demand are usable.

**The house**, per climate system, is learned too, once a day is over, from its rooms with a sensor, an outdoor temperature (the outdoor reference, else the pump's own sensor), the system's water (the mean of its supply and return) and the sun and wind as forecast. Hours with a fireplace on, a room aired (a window open, or a fall of a degree within ten minutes) or the pump's heating stop holding heating off are left out. A house is two parts: the air in its rooms, which follows quickly, and its walls and floors, which change slowly. What comes out:

- **how fast the house loses its heat**, its time constant: with no heat, how many hours it takes to lose most of a degree above the outdoor temperature;
- **how long the heating takes to show in the rooms**: once proved, the planner aims a change of comfort that many hours early, instead of the setup answer's.

It is judged by following the rooms six hours ahead on its last two days, against assuming they stay as they are. Nothing is claimed while the heating has only followed the weather (what it gives and what the house loses can't be told apart then), while the rooms have hardly moved, or for a time constant outside 3 to 500 hours. While the heating stop holds heating off half the time or more, a model that had proved itself is kept, frozen, and still used; once heating is back, it stays in use until there are hours enough to judge it again.

**The pump's curve**, per climate system, is learned from the same days, with or without a room sensor: how the supply temperature the pump calculates follows the outdoor temperature and the heating offset, every hour the pump may heat, however short its runs (a pump that doesn't report its calculated supply: the supply measured in the hours the compressor heated the house for at least half the hour), that is, how many degrees one step of the offset adds. It needs the offset to have stood at two values or more for 12 hours each, as moving heat by price does, and is judged on its last four days against the curve alone. Once proved, a climate system without a room sensor whose heating takes two hours or more to show (radiators, or anything slower, by the answer under House) heats ahead of the weather: the offset is set now to what the curve would need for the outdoor temperature forecast that many hours ahead, by at most three steps, added to the price shift and kept within the household's bound for that system. It is never raised during quiet hours or over a power limit.

**The heating water**, the mean of the supply and the return, is learned over every hour the pump may heat, running or not: how it follows the outdoor temperature and the offset, how much of it carries over from one hour to the next (a slab holds its water for hours), and how much warmer the supply runs than it while the compressor heats. The house model is driven by this water, so the optimizer steers the rooms through it. It is fitted on following the water six hours ahead, as the optimizer follows it: a slab's water barely moves in an hour, and fitted only an hour ahead it can come out holding nearly all of it, with an offset step worth twice what the curve gives, so that the optimizer would believe the floor can hardly be warmed ahead. It is judged the same way on the last four days, against the same fit without the offset.

**The compressor's electricity**, per pump, is learned from the house's power reading once a day is over. When the compressor starts, the house's power rises by what it draws, its circulation pumps with it; when it stops, it falls by as much. Each step is the mean over five minutes after the start or stop less the five minutes before, the minute and a half right at it left out; a start or stop with another one, or the addition, within six and a half minutes isn't read. The draw is the median of the last four weeks' steps, for heating, making hot water and heating the pool apart, each once seen ten times, so the household's own load coming and going doesn't count. It is judged on the last two days' steps against the 2 kW the planner otherwise reckons a hot-water charge draws; once proved, the planner uses it instead, to keep a charge it starts under a power limit. How often the compressor starts is shown too, as the mean of the last seven days. Where an electricity meter on the pump's supply is set up (under Sensors; most pumps have none), the draw is read from that instead, once there are enough stretches: the energy it counted over each stretch the compressor ran on one purpose (heating, hot water, the pool), from ten minutes on, its first two minutes left out, less what the pump draws standing still. Between two of the meter's readings it is taken to rise evenly. With no household load in it, and switches from heating to hot water counted too, it needs far fewer days; it also shows what the pump uses a day, and how much of it while the addition ran. Both need the compressor's state, the pump's demand and a power reading (an electricity meter on the pump's supply, or the whole house's) to have passed their checks.

**The COP**, per pump, is how much heat a kWh of electricity gives, described as a share of the ideal (Carnot's) between the brine coming in and the water made: Nibe's own figures for the F1245 come to about half of it. It starts from the documented figures (under House) and is learned once the compressor's draw has proved itself, each day once over:

- **making hot water**, from charges with no draw near them: the tank's rise (the mean of its two sensors) times its heat per degree, plus its standing loss, against the electricity a meter on the pump's supply counted, or where there is none, what the compressor drew while it ran on hot water;
- **the brine**, from the same charges: what didn't come from electricity came from the ground, so the brine's heat per degree of its drop, its flow in effect, is measured without knowing the brine pump's flow;
- **heating**, from the hours the compressor heated the house, once the brine has proved itself: the ground's heat from the brine's drop, plus the draw, against the draw.

Each is judged on every charge or hour from the ones before it only, against the documented figure (or, with none, the median seen). As the tank's heat is reckoned from two sensors, the COP read from it can come out a little low. The COP is kept for the optimizer; the brine can be pinned in W per kelvin.

**What the radiators or floor give**, per climate system with a room sensor, in kW per degree of water over the room, and **what they store**, in kWh per degree of their own water: what heating the house costs. A slab in the floor stores many kWh per degree, and heats the rooms with them as it cools; radiators store little. Nothing measures the heat, but in the hours the compressor heated it can be worked out, from its electricity (as metered, else the draw times how long it ran) and its COP there; in the hours it didn't heat at all, it is none. They are learned only where the pump heats one climate system, since the heat can't be split between two, and judged on the last four days against the mean heat of the hours before. The output can be pinned.

**A pool** is learned from its temperature, the outdoor temperature and the hours the compressor ran on it, once a day is over: how many degrees an hour of the compressor's heat gives it, and how much of its lead over the outdoor temperature it loses an hour. It is judged on its last four days against the pool staying as it was.

**How far the forecasts miss**, per weather provider, from what Weather keeps (its temperature forecast 1, 6, 24 and 48 hours ahead beside the outdoor reference measured then, see Weather): the **bias**, how far it runs warm or cold on average at each lead time, is judged on the last week against the forecast as given, with the bias from the weeks before; once proved, the planner takes it off the temperature it heats ahead for (in between the lead times, by straight lines). The **spread**, how far the forecast missed nine times in ten over 30 days, is shown and kept for the optimizer's margin on goals marked must.

**Suggested from the week**: once a tank's week has proved itself, the hours when hot water is drawn most, week after week, that no hot-water time covers yet, are offered as hot-water times: "Hot water at 50 °C by 07:00 on Monday, Tuesday, …", at the tank's hot-water level where there is one, else at its stop temperature. **Ask for it** asks for it as a standing intent, as the Intents page would, with your rights; **Not now** doesn't offer it again for eight weeks. Nothing is applied unless asked for.

**What it has learned** lists each value, how many observations it rests on, and its state; the week is drawn hour by hour. A learned model is used only once it has shown it predicts better than a simple guess; until then the answer given at setup, or the planner's own rule, stands. The week is judged each day before it learns from it, against a flat guess; the standing loss on its last two days, fitted on the days before. Once both have proved themselves, the planner reckons how warm the tank's top will be by a hot-water deadline from them, instead of assuming it falls half a degree an hour. A few figures are used as seen: how long the tank's charges take (used to choose the cheapest time for one), when the pump's periodic hot-water increase was last seen, and the warmest a charge has made the top. **Pin** sets a number to use instead (a duration in minutes, the standing loss as its fall an hour at 50 °C), and learning goes on beside it; pinning an empty field lifts it. **Start over** forgets what was learned. Both are recorded in the audit log.

### From NibePi

Choose NibePi's `config.json` and **Read it**. The page shows what it would make: the pump's connection, the MQTT broker and Home Assistant discovery, the location and its time zone, sensors and rooms, and prices (the spot price for NibePi's area, or Tibber), with the MQTT password and the Tibber token kept in the secrets file. NibePi's VAT and fees are offered unticked, since they may be out of date. Untick what you don't want, and **Make it**: the password is asked again, and each part is made as its own topic would make it. If NibePi's broker ran on NibePi's own host, the page says whether one answers on Thermaestro's.

**NibePi's hot-water profile**, below, reads `vv_ai_profile.json`, NibePi's learned week of hot-water use, in the time zone NibePi ran in, and shows it hour by hour; **Start the week from it** makes it where the tank's week starts (see [Coming from NibePi](nibepi.md)).

**Every key in the file** lists what became of each setting, and why one was left out. Nothing is written to the pump. `thermaestro import nibepi config.json` shows the same on the host, and makes nothing. The guide is [Coming from NibePi](nibepi.md).

**The pump's settings after NibePi**, below, is for once NibePi is stopped: say so with **NibePi is stopped**, then **Read the pump's settings**. Each register NibePi is known to change, that the pump has, is listed with what NibePi did, its value now, its factory default and, where NibePi kept it, its value before NibePi. Keep it (nothing to do), or set it to its default, its value before NibePi, NibePi's own heating offset, or a value you enter: each change needs the password again (`device.write`), and is read back and recorded. For the heating offset and the hot-water mode, **Thermaestro puts it back to** sets what Thermaestro restores them to instead. **Compare every setting** reads every setting the pump lets be changed, about a second each, and lists those that differ from the factory default, with the same choices. Only a pump on Nibe's bus, not yet an S-series pump.

## What the household wants: the Intents page

**Intents** in the menu says what applies now, lists what has been asked for, with when each ends and who asked, and takes new requests. Each one asked for folds out to show what it holds: its bands, temperatures and times, the levels it picks, how much it matters, its state and when it was asked. **Change** under it opens a form filled in with what it holds; **Save** changes it in place, checked as a new request would be. Whoever may end an intent may change it. What Thermaestro first took from how the pump runs is marked; **Keep it** makes it the household's own, as changing it does.

- **For a while** (also on the overview): warmer or cooler, until the pattern next changes, at most a day; a bath by a time; one extra charge of hot water now; a fireplace; away until a time, with a level for each climate system; guests; hands off for up to 48 hours. Each ends by itself, and **End** ends it sooner.
  - It shows once there is something to control: a climate system or a hot-water tank. After a restart, that is once the pump has been read.
  - The usual ones are buttons: **Warmer**, **Cooler**, **Extra hot water**, **Away** and **Fireplace**.
  - Anything else can be said in words, in English, Swedish or German: "a bath at 19:30", "borta till söndag", "Gäste bis morgen 18 Uhr", "lite varmare till 22", "hands off for 2 days". Thermaestro reads it with rules of its own (nothing is sent anywhere) and answers with what it understood, as a form to check and change; nothing is asked until **Ask for it**. What it can't tell, such as when you're back, is left for you to fill in, and what it doesn't understand is answered with examples.
- **Always:** comfort, as a band on a weekly pattern of levels (or, for a climate system without a room sensor, how far price may move its heat); hot water ready by times, and the lowest it may get; cost and comfort, the savings slider and what gives way first; the addition; the pool; a power limit; quiet hours.
- **Levels:** the household's own named bands and hot-water tops. **Change** under each one renames or moves it; every intent that names it follows. A level used by an intent can't be removed.

Asking answers at once: until when it applies, what it sets aside, and what the house can't do ("no room sensor: normal heat, shifted by price within a bound instead of a band"). Where [the optimizer](optimizer.md) plans it, the answer also says what a plan made with it makes of it: ready by its time, or about how late and how warm the water would be at its time instead ("Ready about 40 minutes late, at 20:10; or 46 °C at 19:30"), a room that won't reach its band and how warm it will be, another request that gives way for it, and that the plan firms up once tomorrow's prices are out. The answer is in your language. The household's group may ask for things for a while; changing what always applies needs more rights ([the rights](api.md#rights)).

Where [the optimizer](optimizer.md) plans an intent, its state says how it stands in the plan: **at risk** where the plan expects a miss (a room below its band for an hour or more, once its heat has had time to show, or the hot water short at a deadline), **giving way** where the miss is a higher-ranked goal's doing, named ("Gives way to comfort: the plan has the hot water at 46 °C at 19:30, short of 50 °C"). An intent that has ended keeps what came of it: a bath missed says how warm the water was at its latest, and warmer, guests or away whether its rooms kept their band, and if not, for how many minutes they didn't.

## What Thermaestro decides: the Plan page

**Plan** in the menu shows what the planner decided in its last round, for each setting: what it asked, why (in household terms, with what it serves: the hot water's floor, a ranked goal, or price), whether the optimizer decided it, and what became of it. Then:

- **The plan ahead:** the optimizer's plan for the next two days as a chart (the price, dashed where it is estimated; the rooms each climate system is planned to keep; the tank; the outdoor temperature; what the pump will use), and what the plan leaves short, in the order things give way.
- **Beside the rules:** what each planner asked in the last slot, the one driving in bold; how many changes each asked for in the last week; and how the optimizer's predictions of the rooms came out one, three and six hours ahead.
- The hot-water deadlines ahead with the cheapest time to charge for each, the house's power against its limit (the household's, or the grid company's where lower, with why), what shadow would have done, and every change asked in the last day.

Both planners decide every 15 minutes; [the optimizer](optimizer.md) says how it plans.

## What happened: the Events page

**Events** in the menu lists what happened that you may want to know, as it stands now and over the last week. Each is raised once while it lasts and cleared when it ends:

- an intent at risk, or giving way, in the plan;
- a room below its band for over half an hour (the 30 minutes a band may be missed), once its heat has had time to show since the band last rose;
- the hot water below its floor;
- a climate system whose rooms have no fresh temperature, so its offset is the pump's own meanwhile;
- a setting let go after a change Thermaestro didn't make;
- every setting put back because the planner stopped answering;
- a plugin, a device or a feed of prices or weather missing for over an hour, or asking for something;
- a device's own warnings, such as the brine near the pump's alarm limit.

From 07:00 the top of the page sums up the day before, midnight to midnight in the house's time: what the pump used and what it cost; each intent that ended, with what came of it; each request for a while that ran, with what the pump used while it ran and what that cost; and the minutes each room was below its band and the hot water below its floor. The energy comes from an electricity meter on the pump's supply where one is set up; else it is estimated for the time the compressor ran, from its learned draw, or until that has proved itself, from the maker's figures, and the summary says which. What a request used is what the pump used while it ran, not how much more than without it: that needs a reference, still to be built. A summary waits for the pump to be read after a restart, and for the day's prices until noon, then is made without the cost. Days missed while Thermaestro wasn't running are summed up once it runs again, up to a week back; a day before it ran isn't. Where nothing measures the pump's electricity, the summary leaves it out rather than say 0. Summaries are kept for 90 days.

The household sees its own: intents and rooms. Those who may read the settings (Administrators) see the house's workings too, and in the summary how many changes Thermaestro asked for and how often the compressor started.

### Notifications

Nothing is sent to anyone until they turn it on, each on their own page (under their name in the menu), **Notifications**:

- **What:** one's own requests at risk, giving way or missed; everyone's, for those who may end anyone's; a room below its band; the daily summary, all of it in the notification as the Events page shows it; and for those who may read the settings, the house's workings (the floor, settings let go, plugins, device warnings). What a user may turn on follows their rights.
- **Where:** a Home Assistant notification target, such as the phone the Home Assistant app runs on, listed from the Home Assistant set up under **Setup → External**; and an MQTT topic of their own while discovery is on ([the topic](mqtt.md#notifications)). Either, or both.
- Each is sent as it happens, again if what it says changes, and once more when it is over, in the user's language. At most ten an hour reach one person; the rest are held back, and the Events page has them.
- **Send a test** sends one now. What couldn't be sent in the last week, and why, is listed below it.

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

The pages that show how things stand (the overview, the pump, prices, weather, intents, the plan, events and System → Health) keep themselves current: every minute while they are in view, and at once when you come back to them. A form being filled in isn't touched. A page left open still logs out after 12 hours unused.

**Wall displays.** For a screen that shows Thermaestro all the time, such as a tablet on the wall, the account page makes a display link (with the right `wall_displays.own`, which Administrators have). Give it a name and the rights it needs, usually just seeing the values and the plan. Open the link in that screen's browser within a day; it works once, and that browser then stays logged in, without a password typed on it, until you revoke the display on the account page or change your password. A display can't make changes that need a password.

## Checking on it

- **System → Health:** each plugin's state, restarts and health counters, and the HTTPS certificate's fingerprint.
- **System → Audit log:** every change, and who made it. `thermaestro audit verify` checks the log's chain on the host.
- The overview lists what needs attention, and **Events** what happened.
