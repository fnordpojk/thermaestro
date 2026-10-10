# Thermaestro

Thermaestro plans a home's heating and hot water around electricity prices, the weather and what the household wants. The household states its goals, such as warm rooms or hot water by seven, and what may give way when not all of them can be met. Thermaestro works out how, changes only the few settings each device offers, and checks that it happened.

It is brand-neutral: heat pumps, sensors, meters, price sources and weather forecasts come in through plugins. Nibe is the first. It is meant to replace NibePi, and is open source under the AGPL.

**Status:** Thermaestro reads, and changes nothing on a pump yet. It runs read-only beside NibePi on a Nibe F1245. The planner and the safe way of changing settings are built and tested against a simulated house. Setup → Control switches each of a pump's settings to shadow or control, and the API takes the household's intents; pages for entering intents and for the plan, and requests over MQTT, come next. There is no release yet; the Docker Compose file builds it from this repository.

## Features

Built today:

- **Nibe heat pumps.** F-series and related models through a gateway on the pump's accessory bus: plain NibeGW gateways such as esphome-nibe, or the Thermaestro gateway protocol, which reports what happened to every request. S-series over Modbus TCP, from Nibe's documentation, not yet tried on a real pump.
- **Every value with its quality.** A reading says whether it can be used, and why not, and its history is kept.
- **Room and outdoor sensors** from Home Assistant or MQTT, each with its reporting rhythm learned.
- **Electricity prices** without an account in much of Europe (Energy-Charts, Beneficial Apps' Nordic price sites, OMIE), from Tibber or ENTSO-E with your own token, and Octopus Agile in Great Britain. The price is built from its parts: spot price, surcharges, tax, grid fee and VAT.
- **Weather** from MET Norway, SMHI or Open-Meteo, each scored against the house's own outdoor sensor, with Home Assistant as a fallback.
- **Home Assistant:** Thermaestro's state is published over MQTT, with discovery.
- **A web interface** in English, Swedish and German, with logins, users, groups and rights, and an API for every action.
- **A read-only probe** that reports what Thermaestro reads from a pump, for testers.
- **Intents:** the household's goals in its own terms (warm rooms on a weekly pattern, hot water by a time, a lowest hot-water temperature, how much to favor cost over comfort) and requests for a while ("warmer, please", a bath by 19:30, away until Sunday, hands off), kept, checked and ranked. The first ones are taken from how the pump runs.
- **A rule-based planner** that decides every 15 minutes what each setting should be, and why: the curve offset steering the coldest room, hot water kept above its floor and ready by its deadlines, heat moved to cheaper hours as far as the household allows.
- **Safe changes:** one way to change anything, which takes a setting over, checks the value, reads it back, limits how often it changes, and puts it back as found on shutdown or when something goes wrong. In shadow mode it decides the same and changes nothing.
- **Tested against a simulated house**, hot-water tank and pump over simulated days: comfort and the hot-water floor hold, extreme prices buy neither cold nor heat, and shadow decides as control would.

Coming:

- **Control in the web UI and over MQTT:** each setting off, in shadow or in control; intents entered and ended; the plan and its reasons shown.
- **Learning:** models of the house and the hot-water tank, and an optimizer that plans a day or two ahead with them, using the house and the tank as heat storage.
- Grid tariffs as data, and importing NibePi's settings.
- Release images for amd64 and arm64, and packages for Raspberry Pi OS.

## Documentation

- [Setting it up](docs/setup.md)
- [Running it in Docker](docs/docker.md)
- [The read-only probe](docs/probe.md)
- [The HTTP API](docs/api.md)
- [MQTT and Home Assistant](docs/mqtt.md)
- [Writing a plugin](docs/plugins.md)
- [The gateway protocol](docs/gateway-protocol.md)
- [Developing Thermaestro](docs/development.md)

[Contributing](CONTRIBUTING.md) · [Code of conduct](CODE_OF_CONDUCT.md) · [Security policy](SECURITY.md)

## License

AGPL-3.0-or-later. Thermaestro is not affiliated with NIBE Energy Systems.
