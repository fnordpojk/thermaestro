# MQTT and Home Assistant

Thermaestro connects to Home Assistant in two ways:

- it **reads** sensors: from MQTT topics, or from Home Assistant entities through Home Assistant's WebSocket API;
- it **publishes** its values to Home Assistant through MQTT discovery, if switched on.

Nothing is accepted back. Thermaestro takes no commands over MQTT and writes nothing to Home Assistant.

## The broker

One MQTT broker per installation, set under **Setup → External → MQTT broker**, or through the API (`GET` and `PUT /api/v1/mqtt`):

| Field | Default | |
|---|---|---|
| `host` | | the broker's address |
| `port` | 1883 | |
| `username` | none | |
| `password` | none | the *name* of a secret in the secrets file, not the password |
| `tls` | off | TLS, with the broker's certificate checked against the system's trusted certificates |
| `enabled` | on | off: Thermaestro doesn't connect |

The form keeps the password as the secret `mqtt.password`. Over the API, put the password first with `PUT /api/v1/secrets/mqtt.password`, then name it in the broker setting. A secret is never shown back.

Thermaestro connects only while there is something to do: an MQTT sensor to read, or discovery to publish or clear. It connects with a random client id. If the connection fails, it tries again after 1 second, then 2, 4 and so on, up to once a minute. It starts over whenever the broker setting, the sensors or the discovery setting change. The page shows the connection's state (`off`, `connecting`, `connected`, `failed`) and the last error.

## Reading sensors from MQTT

An MQTT sensor is added under **Setup → Sensors → Add an MQTT sensor**. It names:

- **the topic**, such as `zigbee2mqtt/bedroom`. Thermaestro subscribes to it and takes messages whose topic is exactly this one. Wildcards (`+`, `#`) are refused;
- **the JSON field**, for a topic that carries JSON. A dotted name reaches into nested objects (`state.temperature`). Left empty, the whole payload is the value.

A value may be a number, `true` or `false`, or a word:

- on: `on`, `open`, `true`, `yes`, `detected`;
- off: `off`, `closed`, `false`, `no`, `clear`;
- any other text that reads as a number is a number.

Case and surrounding spaces don't matter. A payload that gives no value (not JSON, the field missing, or a word not listed) makes the sensor's value unknown, with the reason "the payload isn't a value".

Example: a sensor with the topic `zigbee2mqtt/sofa` and the JSON field `temperature` reads 21.5 from

```
{"temperature": 21.5, "linkquality": 80}
```

A sensor with the topic `t/hall` and no JSON field reads 19.0 from the payload `19.0`.

Each sensor also has its quantity, its place (a room, outdoors, or elsewhere), a calibration offset that is added to each value, and how long it may stay quiet before its value is stale. Left empty, that time is learned: twice the sensor's longest silence over the last week, at least one hour and at most twelve.

## Reading sensors from Home Assistant

Home Assistant's entities don't come through MQTT. The Home Assistant plugin connects to Home Assistant's WebSocket API (`<url>/api/websocket`) and logs in with a long-lived access token. Make the token in Home Assistant (your profile, Security), and enter it with the address under **Setup → External → Home Assistant**. The token is kept in the secrets file.

The plugin subscribes only to the entities chosen under **Setup → Sensors → From Home Assistant**. It never writes to Home Assistant. Values are converted to Thermaestro's units (°F to °C, mbar to hPa, and so on). Home Assistant's `unavailable` and `unknown` become an unknown value.

## Publishing to Home Assistant: MQTT discovery

Switched on under **Setup → External → Home Assistant discovery**, or with `PUT /api/v1/discovery`. The broker must be set first.

| Field | Default | |
|---|---|---|
| `enabled` | off | |
| `prefix` | `homeassistant` | Home Assistant's discovery prefix, as set in its MQTT integration |
| `base` | `thermaestro` | where the values go |
| `id` | made when first switched on | this installation's part of every topic and unique id, 4 to 16 lowercase letters and digits, so two installations can share a broker. It is kept, and the API ignores a new one |
| `sensors` | off | also publish each sensor's own values; Home Assistant may have them already |
| `language` | `en` | the language of the entity names: `en`, `sv` or `de` |

`prefix` and `base` are one to four topic levels of letters, digits, `_` and `-`.

`GET /api/v1/discovery` returns the setting, the publisher's state (`off`, `sweeping` or `publishing`), and each device published, with its number of entities and how many of them are enabled.

### Topics

With the prefix `homeassistant`, the base `thermaestro` and the id `1a2b3c4d`:

| Topic | Payload | QoS | Retained |
|---|---|---|---|
| `homeassistant/device/thermaestro_1a2b3c4d/config` | Thermaestro's own device | 1 | yes |
| `homeassistant/device/thermaestro_1a2b3c4d_<device>/config` | each device a plugin identifies, such as a heat pump | 1 | yes |
| `thermaestro/1a2b3c4d/status` | `online` or `offline` | 1 | yes |
| `thermaestro/1a2b3c4d/state/<key>` | an entity's state | 0 | yes |
| `thermaestro/1a2b3c4d/attributes/<key>` | an entity's attributes, as JSON | 0 | yes |

Thermaestro also subscribes to `homeassistant/status`. When Home Assistant sends `online` there (it starts), Thermaestro waits a random 0.5 to 3 seconds and sends everything again.

`status` is `online` while Thermaestro publishes. It is also the broker's last will, so the broker sets it to `offline` if Thermaestro's connection is lost. Thermaestro sets it to `offline` itself when it stops publishing. Every entity uses it for availability.

### Keys and unique ids

A `<key>` is made from the plugin instance and the point's path, so it stays the same across restarts: the path in lowercase with every run of other characters as `_`, cut at 48 characters, then `_` and the first six hex digits of the SHA-256 of the original text. The hash keeps `a.b` and `a_b` apart. For example:

| What | Key |
|---|---|
| the pump's outdoor temperature (instance `pump`, point `hp1/outdoor.temp`) | `pump_hp1_outdoor_temp_0000ad` |
| a room's temperature (`site`, `room.living/temperature`) | `site_room_living_temperature_6e8a7e` |
| the plugin `pump`'s state | `status_pump_eaa124` |
| the electricity price | `thermaestro_price_92d16f` |
| needs attention | `thermaestro_attention_7c5021` |

An entity's unique id is `thermaestro_<id>_<key>`. A device's object id is `thermaestro_<id>` for Thermaestro, and `thermaestro_<id>_<key of the instance and node>` for a device a plugin identifies.

### Devices

**Thermaestro** is one device (manufacturer and model "Thermaestro", with its version). It has:

- each room's values and the outdoor values (`room.<room>/<quantity>`, `outdoor/<quantity>`, with dew point and absolute humidity where temperature and humidity meet);
- each sensor's own values, if `sensors` is on, those taken from Home Assistant entities included;
- **Plugin `<name>`**: one per plugin instance, diagnostic, an enum of `starting`, `up`, `restarting`, `waiting`, `stopped`;
- **Needs attention**: diagnostic, a `problem` binary sensor, on when a plugin isn't up, reports an active device event (such as an alarm), or reports that it is down, stale or needs the household to act;
- **Electricity price**, if prices are set up: the price now, in the price's unit (such as `SEK/kWh`), with the attributes `today` and `tomorrow`, lists of `{"start": <ISO time>, "price": <number>}`, and `attribution`, the credit the price sources' terms ask for, where they ask for one. It is worked out again once a minute.

**Each device a plugin identifies** (a heat pump) is a device of its own, connected via Thermaestro (`via_device`). Its name is the household's own name for it if one was given, else the vendor and the product name, such as "Nibe F1245-10 PC". It carries the vendor, model, firmware (`sw_version`) and serial number where the plugin gives them. Each of its points becomes an entity on it.

Rooms aren't devices: Home Assistant has areas for them. The Home Assistant plugin's own points aren't published.

### Entities

Each point becomes one entity. Its name is the one the web pages show, in the chosen language. Which kind it is comes from the point:

- **a point with named values** (an enum): a `sensor` with the device class `enum` and those values as its `options`. A value outside them is sent as unknown;
- **an on/off value**: a `binary_sensor`. `window.open` gets the device class `window`, `zone.open` gets `opening`, and other standard on/off quantities their own;
- **a number**: a `sensor` with the unit in Home Assistant's spelling (`°C`, `m³/h`, `°C·min`) and a device class from the unit where the unit has only one meaning (`°C` temperature, `kW` power, `kWh` energy, `Hz` frequency, and so on), or from the unit and quantity together (`%` humidity, `ppm` carbon dioxide). Counters and energy are `total_increasing`, durations have no state class, and other numbers are `measurement`. The display precision follows the point's resolution;
- **anything else**: a text `sensor`, cut to 255 characters.

Which kind a point is shows only once it has a value, so a point that has never had one isn't published yet.

The pump's settings and diagnostic values (points with a category, the plugin's or the one the household chose) are published with `entity_category: diagnostic` and **disabled**. Enable those you want in Home Assistant.

### States

- A number is sent with as many decimals as the point's resolution gives (`-3.5`), or else rounded to three decimals (`21.25`).
- An on/off value is `ON` or `OFF`.
- A value that isn't of good quality (stale, unknown, uncertain) is sent as `None`, which Home Assistant shows as unknown.
- Each entity's state goes out at most every 10 seconds, and only when it changed. A pump pushes some values twice a second.

### An example

The discovery message of Thermaestro's own device, shortened to one entity:

```json
{
  "device": {
    "identifiers": ["thermaestro_1a2b3c4d"],
    "name": "Thermaestro",
    "manufacturer": "Thermaestro",
    "model": "Thermaestro",
    "sw_version": "0.0.0"
  },
  "origin": {
    "name": "Thermaestro",
    "sw_version": "0.0.0",
    "support_url": "https://github.com/fnordpojk/thermaestro"
  },
  "availability": [{"topic": "thermaestro/1a2b3c4d/status"}],
  "components": {
    "site_room_living_temperature_6e8a7e": {
      "platform": "sensor",
      "unique_id": "thermaestro_1a2b3c4d_site_room_living_temperature_6e8a7e",
      "name": "Living room · Temperature",
      "state_topic": "thermaestro/1a2b3c4d/state/site_room_living_temperature_6e8a7e",
      "device_class": "temperature",
      "unit_of_measurement": "°C",
      "state_class": "measurement"
    }
  }
}
```

The state topic then carries `21.25`.

### Changes and cleanup

- When devices or entities change, only the changed devices' discovery messages are sent again. A removed entity is first sent with only its `platform`, which has Home Assistant delete it. A removed device gets an empty message.
- On every connection Thermaestro reads its own retained topics for two seconds and clears those it no longer publishes. Other installations' topics are left alone.
- Switched off, or with a new prefix or base, Thermaestro clears every retained topic it published under the old ones.

## Not there yet

- Commands over MQTT. Thermaestro accepts nothing from MQTT except Home Assistant's `online`.
- A certificate authority of your own for the broker's TLS: the certificate is checked against the system's trusted certificates only.
