# Writing a plugin

Thermaestro reaches every device and data source through a plugin. A heat pump, a room sensor, a price source and a weather forecast are all plugins, and the core talks to each of them the same way. This page describes that interface for someone writing a plugin.

The code is in [`src/thermaestro/cap/`](../src/thermaestro/cap/). The message types are in `messages.py`, and what a plugin describes is in `model.py`.

## The model

A plugin speaks for its devices in four terms only: nodes, points, levers and series. The core never sees registers, frames or URLs.

### Nodes: the device tree

A plugin describes its devices as a tree of **nodes**. Each node has a path, a kind and a presence:

- **Path:** slash-separated, such as `hp1`, `hp1/cs1` or `hp1/dhw`. The plugin chooses it and keeps it the same across restarts and updates. A node's parent must be described too.
- **Kind:** `site`, `unit`, `climate_system`, `dhw_tank`, `pool`, `compressor`, `addition`, `brine_circuit`, `ventilation`, `room`, `meter` or `output`. A kind outside this list goes under the plugin's own namespace (see [Names](#names)).
- **Presence:** how the node is known to exist: `detected` (the rule that detected it must be named), `configured` or `assumed`. A register that answers is not proof: some pumps answer for hardware that isn't fitted.

A `unit` node (a heat pump, for example) also carries an **identity**: vendor, model, firmware, serial number, and the register map the plugin resolved for it. It may also carry the **promises** of its connection: whether a write's fate is known, what the device's acknowledgment means, whether other clients' writes can be seen, call limits, and whether the connection can restore settings by itself if the core goes silent.

A node may list **shared constraints** with its siblings, such as `one_demand_at_a_time` for demands that compete for one compressor.

### Points: values to observe

A **point** is a value the plugin can report: a temperature, a state, a counter. It has a path under a node (`hp1/dhw/temp.top`), a unit, and how its values arrive:

- `pushed`, with the interval;
- `polled`, with the cost of one read in seconds;
- `on_change`.

A point can also state its resolution, range, enum values, how old a value may get before it is stale (`freshness_s`), the validity rules the plugin applies, and, for a counter, the value at which it wraps to 0.

Every value arrives in an **envelope**:

| Field | Meaning |
|---|---|
| `point` | the point's path |
| `value` | the value, or null |
| `unit` | its unit |
| `raw` | the device's value before scaling, for tracing; never planned on |
| `t_observed` | when the device sampled it, as close to the source as the connection allows; null if unknown |
| `t_received` | when the plugin got it |
| `quality` | see below |
| `source` | `measured`, `calculated`, `estimated` or `assumed` |
| `why` | a short reason when the quality isn't `good` |

The **quality** is one of `good`, `stale`, `not_connected`, `no_flow`, `transitional`, `assumed`, `out_of_range` or `unknown`. Only `good` may drive a decision; anything else is shown and logged. A `good` value must have a value, and a `not_connected` one must not.

### Levers: settings to act with

A **lever** is something the core can change. It has one of four kinds:

| Kind | What it is | Operations |
|---|---|---|
| `setting` | a value that stays until changed | `set` |
| `hold` | a state engaged and then released, restoring what was there | `engage`, `release` |
| `trigger` | a one-shot request the device completes by itself | `fire`, `cancel` |
| `feed` | a value supplied continuously, which lapses if not renewed | `feed` |

A lever must list the datapoints it writes, in the plugin's own terms (`touches`). Two levers whose lists overlap can't be taken over together. A lever that can't be used on this connection says why in `unavailable` instead.

A lever also says how a change can be checked (`verify`):

- `readback`: read a point and compare;
- `effect`: watch a point for a stated expectation;
- `none`: it can't be checked.

The rest of a lever's description says how it behaves: its parameters and their ranges, whether it works at all, its preconditions, whether it is stored or volatile or leased, what happens when a lease lapses, wear (such as flash memory), the shortest time between changes, the delay before its effect shows, features of the device that move the same thing, and anything outside software that overrides it.

### Knowledge

Most facts about a device are wrapped in a **knowledge** object: a value, how it is known, and the basis for it.

| `known` | Meaning |
|---|---|
| `documented` | from a vendor document |
| `verified` | tested on this model and firmware |
| `observed` | seen in normal running |
| `user` | confirmed by the user |
| `reported` | from a third-party source |
| `refuted` | tested and found false |
| `unknown` | not known; carries no value |

`basis` says where the fact came from: a document and page, a test and its date, a model and firmware. Sources that disagree are all listed.

Any knowledge may make the core more careful. Only `documented`, `verified` or `user` knowledge may make it less careful. An unknown field is treated conservatively: a lever whose `works` isn't trusted is never used unattended, unknown persistence counts as stored, and unknown wear counts as flash wear. The rules are in [`cap/defaults.py`](../src/thermaestro/cap/defaults.py).

### Series: prices, grid rules and forecasts

A **series** is a sequence of intervals over time. It is described by a `SeriesInfo`:

- `id`: unique within the plugin;
- `kind`: `price`, `rule` or `forecast`;
- `role`: for a price, the layer of the price it is, such as `energy.spot`; the forecast plugins use `weather`;
- `unit`, and `vat` (`incl`, `excl` or `n/a`);
- `resolution`, an ISO 8601 duration such as `PT15M`;
- optionally the area, the daily publication time, the horizon, and for a forecast its quantity, time steps, weather models, update rate and percentiles.

Each **interval** has a start, an end, a value, a unit, a status (`final`, `preliminary`, `estimate` or `forecast`) and a revision number. A changed value for the same interval is sent again with a higher revision.

A plugin that offers series also describes its **provider**: who runs the source, what it covers, whether a key is needed, rate limits, and the license and attribution the user interface must show.

### Names

Standard names make points and levers findable. They are listed in [`cap/vocabulary.py`](../src/thermaestro/cap/vocabulary.py):

- **Points**, with their unit and the node kinds they belong on: `outdoor.temp`, `supply.temp`, `temp.top`, `demand`, `degree_minutes`, `heat.produced`, and so on.
- **Sensor quantities**, by Home Assistant's sensor device classes, with the unit Thermaestro stores: `temperature` in `degC`, `humidity` in `%`, `carbon_dioxide` in `ppm`, and so on. Plugins convert.
- **Forecast quantities**, with the unit a forecast series gives them in.
- **On/off states**, by Home Assistant's binary sensor device classes.
- **Levers**, with the kinds each may have: `heating.offset` is a setting, `dhw.block` a hold, `dhw.boost_once` a trigger. A name ending in `_input`, such as `room.temp_input`, is a feed.

Names are `area.quantity`. On a node of the area's own kind the area may be left out: `dhw.block` on a hot-tap-water tank is written `hp1/dhw/block`. A name may carry qualifiers in braces, `heat.produced{purpose=dhw}`, and a suffix telling several of the same apart, `temperature#2`.

Anything outside the vocabulary goes under the plugin's own namespace, `x.<plugin>.<name>`, such as `x.nibe.47134`. The core logs and shows it, but never plans on it. Give such a point a `label` so people can tell what it is.

## The messages

Every request carries an `id` the core chooses, and everything that answers it carries the same `id`. In process, messages are passed as objects. Over a socket, each message is one JSON object on one line, in UTF-8. A line may be at most 1 MiB.

### From the core to a plugin

| Message | Answer |
|---|---|
| `hello` | the plugin's own `hello` |
| `describe` | `described`, complete |
| `read` | `values`; with `after`, only values sampled after that time |
| `subscribe` | `update` messages until `unsubscribe` |
| `unsubscribe` | none; ends the subscription with this `id` |
| `act` | one or more `fate` messages |
| `series.get` | `series.data` |
| `series.subscribe` | `series.update` messages until `unsubscribe` |
| `rules.get` | `rules` |

### From a plugin to the core, unasked

| Message | What it says |
|---|---|
| `health` | the plugin's or a unit's state: `up`, `down` or `contended`; last traffic; counters; why a person must act; series whose publication is late |
| `device_event` | an alarm or warning from a device, and when it ends |
| `foreign_write` | another client wrote a datapoint, where the connection can see it |
| `described` | changed parts of the description, with `complete` false and no `id` |

### Fates

An `act` is answered by its fates, in order: optionally `queued`, then optionally `sent`, then one final stage: `device_accepted`, `device_refused`, `dropped` or `unknown`. Accepted isn't applied: whether a change took is the core's to decide, using the lever's `verify`.

### Errors

A plugin answers a request it can't serve with `error`, with the code `version`, `unsupported` or `invalid`. Two requests have their own way to fail instead:

- a read of an unknown point answers with quality `unknown`;
- an act on an unknown lever ends with fate `dropped`, saying why.

### Examples

These lines were produced by the message types themselves:

```json
{"type":"hello","id":1,"protocol":"thermaestro-cap","version":"0.2","role":"core","plugin":null,"plugin_version":null,"features":["subscribe","forecast"]}
{"type":"hello","id":1,"protocol":"thermaestro-cap","version":"0.2","role":"plugin","plugin":"omie","plugin_version":"0.1.0","features":["subscribe"]}
{"type":"read","id":3,"points":["hp1/dhw/temp.top"],"after":null}
{"type":"values","id":3,"values":[{"point":"hp1/dhw/temp.top","value":52.3,"unit":"degC","raw":null,"t_observed":"2026-10-09T12:00:00Z","t_received":"2026-10-09T12:00:00Z","quality":"good","source":"measured","resolution":null,"why":null}]}
{"type":"act","id":4,"lever":"hp1/dhw/block","op":"engage","params":{}}
{"type":"fate","id":4,"stage":"device_accepted","t":"2026-10-09T12:00:00Z","detail":null}
{"type":"series.get","id":5,"series":"spot","from":"2026-10-09T12:00:00Z","to":"2026-10-09T12:00:00Z"}
{"type":"error","id":6,"code":"unsupported","detail":"not offered"}
```

Times are ISO 8601 with a time zone. Fields with defaults may be left out.

### The JSON Schema

For plugins written in other languages, the JSON Schema of every message is [`src/thermaestro/cap/data/thermaestro-cap-0.2.schema.json`](../src/thermaestro/cap/data/thermaestro-cap-0.2.schema.json). It is generated from the message types:

```sh
uv run python scripts/make-capschema.py          # write it
uv run python scripts/make-capschema.py --check  # fail if it is out of date
```

The test suite fails if the checked-in copy differs from the types.

### Versions

The protocol is `thermaestro-cap`, version `0.2`. The core says `hello` first, and the plugin answers with its own, naming itself (`plugin`, matching `^[a-z0-9_]+$`) and its version. Major versions must match; a plugin answers a different major version with error `version`. A plugin speaking `0.1` still talks to a `0.2` core: `0.2` added the provider description and the forecast fields of `SeriesInfo`.

## Running a plugin

A plugin implements the `Plugin` protocol in [`cap/plugin.py`](../src/thermaestro/cap/plugin.py):

```python
class Plugin(Protocol):
    name: str
    version: str
    features: tuple[str, ...]

    async def handle(self, request: Message, send: Send) -> None: ...
    async def events(self, send: Send) -> None: ...
```

It is run with `serve(endpoint, plugin)`, which:

- answers `hello`, checking the protocol and major version;
- answers lines that aren't messages, and messages meant for the core, with `error`;
- runs each other request as its own task with `handle`, so a slow answer holds up nothing else;
- cancels a subscription's task on `unsubscribe`;
- runs `events` for as long as the connection lasts, for what the plugin sends unasked.

A subscription's `handle` runs for as long as the subscription lasts, sending updates, and is cancelled when it ends.

### What the core does with a plugin

Once a plugin has said `hello`, the core:

1. asks `describe`, waiting up to 60 seconds, since a plugin may first have to identify its device;
2. subscribes to every described point, and keeps the values;
3. for each series, asks `series.get` from a day back to two days ahead, then `series.subscribe`;
4. applies each partial `described` it receives, replacing changed items and dropping removed paths.

If a plugin fails or its connection ends, the core restarts the instance after a pause that grows from 1 second to at most 60, and records the failure in the audit log. A session that lasted a minute resets the count. One plugin failing never stops the others.

### In process

A plugin package registers a factory under the entry-point group `thermaestro.plugins`. The entry point's name is the plugin's name, as an instance's settings refer to it:

```toml
[project.entry-points."thermaestro.plugins"]
omie = "thermaestro.omie.plugin:create"
```

The factory takes a `PluginContext` ([`core/plugins.py`](../src/thermaestro/core/plugins.py)) and returns the plugin:

- `instance`: the instance's id, which tells several instances of one plugin apart;
- `settings`: the instance's settings;
- `secrets`: the secret store, read by name; a setting holds a secret's name, never its value;
- `state`: a JSON object of the plugin's own, kept in the database across restarts.

The core connects to an in-process plugin through a pair of in-memory endpoints (`cap.pair()`), with the same messages as over a socket.

### Out of process

A plugin with no factory runs as its own process and connects to the core. The core listens only if the start-up file names a socket:

```toml
[plugins]
socket = "/run/thermaestro/plugins.sock"
```

On a Unix socket ([`cap/sockets.py`](../src/thermaestro/cap/sockets.py)):

- the socket sits in a directory only the service user can enter (0700, or 0750 for a group of plugins), and the socket file is created so only that user can connect;
- each connection's peer credentials are checked: by default only a process running as the core's own user is taken.

Where there are no Unix sockets with peer credentials, the core listens on TCP `127.0.0.1` instead. It writes the address and a new token to a connection file next to the socket path, with the suffix `.json` (`plugins.json`), readable by the service user only. The plugin's first line must be `{"type":"auth","token":"..."}` with that token; otherwise the core closes the connection without answering.

From Python, `connect_unix(path)` or `connect_tcp(connection_file)` gives an endpoint to pass to `serve`.

The core matches a connecting plugin to a waiting instance by the plugin name in its `hello`. The instance must already exist in the settings, naming the plugin. The protocol carries no settings or secrets: an out-of-process plugin brings its own. There is no page or API call yet that adds an instance of a plugin Thermaestro doesn't ship.

## A plugin, walked through

The simplest real plugins are the day-ahead price sources. [`omie/plugin.py`](../src/thermaestro/omie/plugin.py) fetches Spain's and Portugal's prices from a file OMIE publishes each day. It builds on two shared classes:

- [`seriesplugin.py`](../src/thermaestro/seriesplugin.py): `SeriesPlugin` describes one `site` node with no points and no levers, holds the fetched intervals, answers `series.get` from them, and sends new intervals and revisions to subscribers. It answers `read` and `subscribe` with quality `unknown`, and `act` with fate `dropped`.
- [`dayahead.py`](../src/thermaestro/dayahead.py): `DayAheadPlugin` adds the fetching, in `events`: today's and tomorrow's prices at start, then again once the next day's are due, asking every ten minutes until they come, and backing off when the source fails. It sends `health` every minute, `down` with the reason when fetching fails, and lists the series as stale two hours after their publication time if tomorrow's prices still haven't come.

The OMIE plugin itself only says what the source offers:

- `name`, `version`, `root` (its node's path) and `label`;
- `series_infos()`: one series, `spot`, of kind `price` and role `energy.spot`, in the household's currency per kWh, VAT excluded, at 15 minutes;
- `provider()`: OMIE, its coverage, that no key is needed, and the attribution its legal notice asks for;
- `publication()` and `zone()`: when and in which time zone the prices come;
- `fetch()`: the intervals for the given days, from the source's file;
- `create(context)`: the entry point, which validates the instance's settings.

A plugin for a device is larger, since it describes nodes, points and levers. The Nibe plugin is one; a small invented heat pump is in [`tests/capfake.py`](../tests/capfake.py), written for the conformance tests. It runs in process or, as a script, connects to a socket from its own process.

## The conformance test

[`cap/conformance.py`](../src/thermaestro/cap/conformance.py) plays the core against a plugin and lists what doesn't conform. It checks:

- `hello`, and that a wrong major version is refused with error `version`;
- that the answer to `describe` is complete and the tree is sound: every node's parent is described, every unit has an identity, no path appears twice, every point and lever is on a described node, standard names are used correctly with their units and on the right node kinds, other names are under the plugin's own namespace, and every lever's `verify` point is described;
- `read` of every point, with the described units; a read of an unknown point answers quality `unknown`; a read with `after` returns no value observed before that time;
- an `act` on an unknown lever ends with fate `dropped` and a reason;
- a subscription delivers an update and stops after `unsubscribe`;
- `series.get` for every series, with intervals in order and not overlapping; `series.get` and `rules.get` are answered with error `unsupported` where the plugin offers no series or rules;
- a message meant for the core is answered with error `unsupported`;
- what the plugin sent unasked: events only for described units, partial `described` messages only, no unasked fates;
- that every line was a message.

The only `act` it sends goes to a lever that doesn't exist, unless you name levers to try, so it is meant to be safe against a real device.

To check a plugin in another process, start the suite, then start the plugin so it connects there:

```sh
uv run python -m thermaestro.cap.conformance --unix /tmp/cap/plugins.sock
```

Options:

- `--tcp FILE`: listen on TCP `127.0.0.1` instead, writing the connection file;
- `--timeout S`: seconds to wait for each answer (default 5);
- `--wait S`: seconds to wait for the plugin to connect (default 60);
- `--tries JSON`: levers to act on, such as `[["hp1/dhw/block", "engage", {}]]`.

It prints `FAIL` and the problem for each finding, then `conforms` or the number of problems, and exits with 0 if the plugin conforms.

From Python, `await conformance.run(endpoint)` returns the findings; an empty list means the plugin conforms. [`tests/test_cap_conformance.py`](../tests/test_cap_conformance.py) runs it in process and over both sockets.
