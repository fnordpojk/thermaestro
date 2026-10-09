# Thermaestro

Thermaestro plans a home's heating and hot water around electricity prices, the weather and what the household wants. The household states its goals, such as warm rooms or hot water by seven, and what may give way when not all of them can be met. Thermaestro works out how, changes only the few settings each device offers, and checks that it happened.

It is brand-neutral: heat pumps, sensors, meters, price sources and weather forecasts come in through plugins. Nibe is the first. It is meant to replace NibePi, and is open source under the AGPL.

**Status:** Thermaestro reads, and changes nothing yet. It runs read-only beside NibePi on a Nibe F1245. Planning and control are being built. There is no release yet; the Docker Compose file builds it from this repository.

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

Coming:

- **Intents:** the household's goals in its own terms, ranked, with plain reports when one can't be met.
- **A planner** that looks a day or two ahead in 15-minute steps, using the house and the hot-tap-water tank as heat storage.
- **Safe control:** settings taken over, read back and put back on shutdown, and a shadow mode that shows what it would do.
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

## License

AGPL-3.0-or-later. Thermaestro is not affiliated with NIBE Energy Systems.
