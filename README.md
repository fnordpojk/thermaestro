# Thermaestro

Thermaestro plans a home's heating and hot water around electricity prices, the weather and what the household wants. The household states its goals, such as warm rooms or hot water by seven, and what may give way when not all of them can be met. Thermaestro works out how, changes only the few settings each device offers, and checks that it happened.

It is brand-neutral: heat pumps, sensors, meters, price sources and weather forecasts come in through plugins. Nibe is the first. It is meant to replace NibePi, and is open source under the AGPL.

**Status:** Thermaestro decides, and changes nothing on a pump yet. It runs in shadow beside NibePi on a Nibe F1245, showing what it would change while NibePi stays in control. Two planners, rules and an optimizer, and the safe way of changing settings are built and tested against a simulated house. Setup → Control switches each of a pump's settings to shadow or control, the Intents page takes what the household wants, the Plan page shows what was decided and why, and Home Assistant gets buttons and states for it over MQTT. The rig that tries each setting on a real pump has run on the test pump. Next: at least a week in shadow, then control, a setting at a time. There is no release yet; the Docker Compose file builds it from this repository.

## Features

Built today:

- **Nibe heat pumps.** F-series and related models through a gateway on the pump's accessory bus: plain NibeGW gateways such as esphome-nibe, or the Thermaestro gateway protocol, which reports what happened to every request. S-series over Modbus TCP, from Nibe's documentation, not yet tried on a real pump.
- **Every value with its quality.** A reading says whether it can be used, and why not, and its history is kept.
- **Room and outdoor sensors** from Home Assistant or MQTT, each with its reporting rhythm learned.
- **Electricity prices** without an account in much of Europe (Energy-Charts, Beneficial Apps' Nordic price sites, OMIE), from Tibber or ENTSO-E with your own token, and Octopus Agile in Great Britain. The price is built from its parts: spot price, surcharges, tax, grid fees and VAT. A grid company's rules are entered as data (a time-of-use price, a power charge, a subscribed power); Denmark's tariffs are read from Energi Data Service.
- **Weather** from MET Norway, SMHI or Open-Meteo, each scored against the house's own outdoor sensor, with Home Assistant as a fallback.
- **Home Assistant:** Thermaestro's state is published over MQTT, with discovery, and requests for a while ("warmer, please") come back the same way, with only the rights the administrator grants.
- **A web interface** in English, Swedish and German, with logins, users, groups and rights, and an API for every action. Its pages keep themselves current, and a wall display stays logged in with only the rights given to it.
- **A read-only probe** that reports what Thermaestro reads from a pump, for testers.
- **A rig** that tries each setting Thermaestro changes on a real pump, through the same code control uses: it changes the setting, watches what the pump does, puts it back, and writes down what happened. Read-only unless asked to write.
- **Intents:** the household's goals in its own terms (warm rooms on a weekly pattern, hot water by a time, a lowest hot-water temperature, how much to favor cost over comfort) and requests for a while ("warmer, please", a bath by 19:30, away until Sunday, hands off), kept, checked and ranked. Requests for a while are buttons, or said in words in English, Swedish or German, read by Thermaestro's own rules. The first ones are taken from how the pump runs.
- **A rule-based planner** that decides every 15 minutes what each setting should be, and why: the curve offset steering the coldest room, hot water kept above its floor and ready by its deadlines, heat moved to cheaper hours as far as the household allows.
- **An optimizer** that plans the next two days at once from what has been learned: the house and the hot-water tank as heat storage, the COP at each hour's temperatures, prices past the published ones estimated, what gives way solved rank by rank as the household ranked it. It plans beside the rules every 15 minutes, its decisions kept and shown beside theirs, and drives once the household switches to it, only where its models have proved themselves. With forecasts, prices and hot-water use as uncertain as a household's, on six simulated houses over three days it cost less than the rules in four, from 1 % to 28 % less, and about 3 % more in the other two, and kept the rooms in their band as well (within a few hundredths of a degree-hour) or better in all six; on ten over a week it cost 4 % less in all, and left the rooms below their band for 3 degree-hours against the rules' 35. Told the truth instead (the weather as it came, every price, each day's draws), it did hardly better: 0.6 % less over the week, so what is left to gain is in how well it knows a house, not in its forecasts. It isn't ready to drive yet.
- **Safe changes:** one way to change anything, which takes a setting over, checks the value, reads it back, limits how often it changes, and puts it back as found, or as the household chose, on shutdown or when something goes wrong. A person can also change one of the pump's own settings directly, checked and read back the same way. In shadow mode it decides the same and changes nothing, and each decision is set beside what the pump showed at that moment, as charts, a table and CSV.
- **Control in the web UI and over MQTT:** each setting off, in shadow or in control; what the household wants on an Intents page, each intent and level changed in place, and as quick requests on the overview; a Plan page with what was decided and why, the house's power kept under the household's and the grid company's limits; Home Assistant buttons and states.
- **Coming from NibePi:** its `config.json` read into a draft of the pump's connection, the MQTT broker, sensors, rooms, location, prices and secrets, made once confirmed, with every setting accounted for. Once NibePi is stopped, the pump's own settings NibePi changed are read and set beside their factory defaults and their values before NibePi, to keep or set back one at a time, and every other setting can be compared with its default too. NibePi's learned hot-water profile can be where the tank's week starts.
- **Learning's groundwork:** each value a pump offers is checked against how its quantity behaves before anything learns from it, since an existing register isn't always a measurement; what is learned is kept across restarts, and the household can pin a number or start one over.
- **The house, learned** per climate system from its rooms, its water and the weather: how fast it loses heat, and how long its heating takes to show, which the planner then aims for instead of the setup answer. The pump's curve is learned too, how its supply and its water follow the weather and the offset; a slow climate system without a room sensor then heats ahead of the forecast weather. So is what the radiators or floor give per degree, and how fast a pool warms and cools.
- **The compressor's electricity, learned** from the pump's own meter where it has one (a pulse counter over MQTT or a Home Assistant entity), else the house's power reading: what it draws heating and making hot water, used under a power limit once proved, and how often it starts.
- **The COP, learned:** from the maker's figures (given by the plugin where it knows the model, else entered), then from hot-water charges and the brine's drop: how much heat a kWh gives heating and making hot water, and the brine's flow, measured rather than guessed.
- **Replay:** a period of an installation exported (no accounts, secrets, addresses or coordinates) and the learners run over it again day by day, as they would have run then, with the hot-water use a household noted scored against the draws found.
- **The forecasts' misses, learned** per weather provider and lead time: a bias that has proved itself is taken off the temperature the planner heats ahead for, and the spread is kept for the optimizer.
- **The hot-water tank, learned:** its charges, its draws (from how its two temperatures fall outside the charges), its heat loss and a typical week of use, each day judged before it is learned from; once proved, the planner reckons with them before a hot-water deadline, and the hours of most use are offered as hot-water times, applied only if asked for.
- **Tested against a simulated house**, hot-water tank and pump over simulated days, with either planner driving: comfort and the hot-water floor hold, extreme prices buy neither cold nor heat, and shadow decides as control would.

Coming:

- **Control on the test pump**, after a week of shadow beside NibePi, setting by setting: hot water first, the heating offset after.
- **The optimizer on the test pump:** beside the rules for at least two weeks before the household decides whether it drives; recorded weeks replayed with both planners.
- NibePi's control settings as draft intents.
- Release images for amd64 and arm64, and packages for Raspberry Pi OS.

## Documentation

- [Setting it up](docs/setup.md)
- [Coming from NibePi](docs/nibepi.md)
- [Running it in Docker](docs/docker.md)
- [The read-only probe](docs/probe.md)
- [The rig: trying a setting on a pump](docs/rig.md)
- [The optimizer](docs/optimizer.md)
- [Replaying a recording](docs/replay.md)
- [The HTTP API](docs/api.md)
- [MQTT and Home Assistant](docs/mqtt.md)
- [Writing a plugin](docs/plugins.md)
- [The gateway protocol](docs/gateway-protocol.md)
- [Developing Thermaestro](docs/development.md)

[Contributing](CONTRIBUTING.md) · [Code of conduct](CODE_OF_CONDUCT.md) · [Security policy](SECURITY.md)

## License

AGPL-3.0-or-later. Thermaestro is not affiliated with NIBE Energy Systems.
