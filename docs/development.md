# Developing Thermaestro

How the repository is laid out, how to run Thermaestro and its tests from a checkout, and how to change the files that are generated or translated.

## The repository

| Path | What it holds |
|---|---|
| `src/thermaestro/` | The core, its web UI and API, and the plugins that ship with it |
| `gateway/` | `thermaestro-gateway`, its own package: the gateway between a Nibe pump's RS485 bus and UDP, for a Linux machine. A uv workspace member, with its own tests in `gateway/tests/` |
| `tests/` | The core's tests, the fakes they run against, and the simulated house in `tests/plant/` |
| `testvectors/` | Byte-level vectors for the Nibe bus framing and the gateway protocol, shared with esphome-nibe |
| `scripts/` | Generators for checked-in files, and the license check |
| `docs/` | This and the other documentation |
| `Dockerfile`, `compose.yaml` | The container, built from source ([docker.md](docker.md)) |

The packages under `src/thermaestro/`:

| Package | What it does |
|---|---|
| `cap/` | The plugin protocol: the messages, the device model, the standard point names (`vocabulary.py`), the sockets, and the conformance suite for plugins ([plugins.md](plugins.md)) |
| `core/` | The daemon: the plugin host, the values and their history, sensors, weather, prices, MQTT and Home Assistant discovery, the house model, the write path (`executor.py`) and the audit log |
| `intents/` | What the household wants: intents, levels, how they resolve, and the intents' JSON Schema |
| `store/` | The state database and its migrations, settings, secrets, the start-up file and the directory layout |
| `auth/` | Accounts, passwords, groups and permissions, user preferences, the setup code |
| `web/` | The web UI (FastAPI, Jinja2 templates, htmx) and the API ([api.md](api.md)), translations in `web/locale/` |
| `nibe/` | The Nibe plugin: register maps, the pump families, the transports (plain NibeGW, the Thermaestro gateway protocol, Modbus TCP) and the probe ([probe.md](probe.md)) |
| `homeassistant/` | The Home Assistant plugin, over its WebSocket API |
| `energy_charts/`, `entsoe/`, `nordic_sites/`, `octopus_agile/`, `omie/`, `tibber/` | Price plugins |
| `met_norway/`, `smhi/`, `open_meteo/` | Weather plugins |

The plugins that ship are found through the `thermaestro.plugins` entry points in `pyproject.toml`.

## Getting set up

You need [uv](https://docs.astral.sh/uv/). Thermaestro needs Python 3.13 or later; CI uses 3.13.

```sh
uv sync --all-packages
```

`--all-packages` installs the gateway workspace member too, which the core depends on. `uv sync --python 3.13 --all-packages` matches CI's Python exactly.

### The command line

`uv run thermaestro --help` lists the commands:

| Command | What it does |
|---|---|
| `run` | Run Thermaestro |
| `setup-code` | Print the code for creating the first administrator in the web UI |
| `admin create NAME [--group G]`, `admin reset-password NAME` | Users, from the host: add one (in Administrators unless `--group` says otherwise), or set a password and enable the user |
| `audit verify [FILE]` | Check the audit log's chain |
| `status` | What Thermaestro reads, through its API; an API token in `THERMAESTRO_TOKEN` |
| `nibe-logset MODEL OUTPUT` | Write a LOG.SET for a Nibe bus pump, to copy to a USB stick |
| `probe HOST` | Read a Nibe pump, writing nothing to it ([probe.md](probe.md)) |

`--log-level` (before the command) sets the log level; the default is INFO.

The gateway has its own command: `uv run thermaestro-gateway --help`.

### Running it locally

Thermaestro reads its start-up file from the configuration directory and keeps everything else in the state directory. They default to `/etc/thermaestro` and `/var/lib/thermaestro`. The variables `CONFIGURATION_DIRECTORY` and `STATE_DIRECTORY` move them, as systemd and the Docker image set them. To run from a checkout:

```sh
mkdir -p dev/config
cat > dev/config/thermaestro.toml <<'EOF'
[web]
listen = "127.0.0.1"
port = 8080
https = false
EOF
CONFIGURATION_DIRECTORY=$PWD/dev/config STATE_DIRECTORY=$PWD/dev/state uv run thermaestro run
```

The start-up file is optional; without it, the web UI listens on all addresses, on 8080 for HTTP and 8443 for HTTPS with a certificate of its own. `store/startup.py` lists every key.

On the first start, the database is created and migrated, and the log shows a setup code for creating the first administrator at `http://127.0.0.1:8080`. The code works once, for 60 minutes; `thermaestro setup-code`, with the same variables, shows it or makes a new one. The state directory and its files are made readable by your user only.

## Checks and tests

CI (`.github/workflows/ci.yml`) runs these, in this order. Run them before pushing:

```sh
uv lock --check
uv sync --locked --all-packages
uv run ruff format --check
uv run ruff check
uv run mypy
uv run python scripts/make-testvectors.py --check
uv run pytest
```

- **ruff** formats and lints, with a line length of 100. The rule sets are in `pyproject.toml`, with the few per-file exceptions and why each is there.
- **mypy** runs in strict mode over `src`, `gateway/src`, both test folders and `scripts`.
- **pytest** runs `tests/` and `gateway/tests/`. Warnings are errors, and an `xfail` that passes fails. Async tests need no marker.

`uv run pytest tests/test_core_values.py` runs one file, as usual.

CI also builds the container and starts it as `compose.yaml` does, and runs the backup and move in [docker.md](docker.md). A separate job checks licenses (below).

### The nightly run

Tests marked `@pytest.mark.nightly` are long simulated runs over many scenarios, and are left out by default. `.github/workflows/nightly.yml` runs them each night:

```sh
uv run pytest -m nightly --durations=20
```

### What the tests run against

Nothing in the tests reaches a real device or the internet. They use stand-ins:

| Stand-in | What it plays |
|---|---|
| `gateway/tests/simpump.py` | A Nibe bus pump on a pseudo-terminal: the MODBUS40 side of the bus, read and write tokens, LOG.SET pushes. `OtherClient` is another program on the same gateway. `tests/conftest.py` puts the Python gateway in front of it. |
| `tests/simspump.py` | A Nibe S-series pump: a small Modbus TCP server. Writes are recorded and never applied. |
| `tests/plant/` | A house, a hot-water tank and a pool behind a simulated Nibe pump, in simulated time. See below. |
| `tests/capfake.py` | One invented heat pump, for the plugin conformance suite. `flaws` makes it break one rule at a time. Run as a script, it connects to a core from its own process. |
| `tests/leverfake.py` | An invented device with one lever of each kind, for the write path. It can accept a write but not keep it, refuse one, answer slowly, or be changed by someone else. |
| `tests/hafake.py` | Home Assistant's WebSocket API, as much as the plugin uses. |
| `tests/tibberfake.py`, `tests/entsoefake.py`, `tests/spotfake.py` | The price sources, in their real formats with invented values. `spotfake.py` covers Energy-Charts, Beneficial Apps' Nordic price sites, OMIE, Octopus and the ECB's rates. |
| `tests/weatherfake.py` | MET Norway, SMHI and Open-Meteo. |

**The plant** (`tests/plant/`) is for behavior tests of the whole core. A `Scenario` is drawn from a seed (`draw(seed, emitter=...)`), with the house's parameters taken from plausible ranges, so the core is never tested against its own assumptions. A scenario is plain data and can be saved as JSON to keep a failing case. The model (`plant/model.py`) heats it roughly as an F1245 in auto mode does, and everything the pump shows or is told is a register word, so a write over the bus takes effect.

- `plant/vloop.py` runs the test in simulated time: the event loop's clock jumps ahead whenever nothing is ready, so days pass in seconds. `simulate(main, start_time)` runs a coroutine that way.
- `plant/bus.py` is the pump's bus in process, encoded and parsed by the same code as the gateway.
- `plant/harness.py` runs the core against it: `async with running(Sim(scenario, tmp_path)) as sim:`, then `await sim.advance(seconds)`, `sim.value(point)`, `sim.plant`, `sim.bus.writes`. A `decide` callback stands in for the planner and asks for changes through `Sim.act`.

`tests/test_plant_stack.py` shows the pattern.

## Generated files

These files are checked in and generated. Don't edit them by hand.

| File | Made by | Checked by |
|---|---|---|
| `testvectors/*.json` | `uv run python scripts/make-testvectors.py` | CI, with `--check` |
| `src/thermaestro/cap/data/thermaestro-cap-*.schema.json` | `uv run python scripts/make-capschema.py` | `tests/test_cap_schema.py` |
| `src/thermaestro/intents/data/intent.schema.json` | `uv run python scripts/make-intentschema.py` | `tests/test_intents.py` |
| `src/thermaestro/nibe/maps/data/bus.json`, `s-series.json` | `scripts/make-registermaps.py` | not in CI: the sources stay outside the repository |

Each script also takes `--check`, which fails if the checked-in file differs from what it would write.

- **The test vectors** are built with `struct` and `hmac` directly, not with the codec under test. esphome-nibe tests against the same files. They are available under the MIT License as well (`testvectors/README.md`).
- **The plugin protocol's schema** comes from the message types in `cap/`. Change a message, then run the script.
- **The register maps** need the source files: `--modbusmanager` (NIBE ModbusManager CSV exports), `--nibepi` (NibePi's `models/*.json`), `--nibe-lib` (the nibe library's `nibe/data`) and `--official` (Nibe's S-series register list as `pdftotext -layout` gives it). `src/thermaestro/nibe/maps/data/README.md` names the sources and their licenses.

### Vendored web files

`src/thermaestro/web/static/vendor/` holds htmx and uPlot, with their licenses. `vendor.json` pins each file's SHA-256, and `tests/test_web_vendor.py` fails if a file differs or isn't listed. Update a file and its hash together.

### License check

The runtime dependencies may be permissive, LGPL, MPL-2.0 or GPLv3/AGPLv3; GPLv2-only and EPL are not allowed. CI checks this in an environment without dev dependencies:

```sh
uv sync --locked --no-dev --all-packages
scripts/check-licenses.sh
uv sync --all-packages   # the dev dependencies back
```

A license string the script doesn't know fails the check, and needs a person to look at it.

## Translations

The web UI is in English, Swedish and German. The messages are marked in code with `_()`, `gettext`, `ngettext`, and `mark()` for text kept in a table and translated where it is shown (`web/i18n.py`, as in `web/labels.py`), and in templates with `{{ _("...") }}`. `babel.cfg` says which files are read.

The catalogs are in `src/thermaestro/web/locale/`: the template `messages.pot` and `sv/LC_MESSAGES/messages.po` and `de/LC_MESSAGES/messages.po`. Thermaestro reads the `.po` files when it starts, so there are no `.mo` files to compile.

After adding, changing or removing a message:

```sh
uv run pybabel extract -F babel.cfg -k mark --no-location --sort-output \
    -o src/thermaestro/web/locale/messages.pot src/thermaestro
uv run pybabel update -w 88 -i src/thermaestro/web/locale/messages.pot \
    -d src/thermaestro/web/locale
```

`-w 88` keeps the catalogs' current line wrapping. Then translate the new entries in both `.po` files, and clear any entry `pybabel update` marks `fuzzy`. `tests/test_web_i18n.py` fails if the template is missing a message from the code, still has one the code no longer uses, or if a language lacks a translation or has a fuzzy one.

## The database and its migrations

The state is one SQLite database (`store/database.py`). Its schema is versioned: `MIGRATIONS` is a tuple of SQL scripts, the database's `user_version` says how many have run, and on opening, each one not yet run runs once, in its own transaction. A database from a newer release is refused.

To change the schema, append a script to `MIGRATIONS`, with a comment saying what it is for. Never change one that has shipped.

- **Use `IF NOT EXISTS`** (`CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`) and `INSERT OR IGNORE`. The migration tests set an existing database's version back and open it again, so later migrations run a second time on tables that already exist.
- **A removed or renamed settings field needs a migration.** Settings are stored as JSON documents, and every settings model forbids fields it doesn't know (`extra="forbid"` in `store/settings.py`). A field left behind in stored settings makes them fail to load. Remove or rename it with `json_remove` or `json_set` in a migration, as migration 7 does, and add a test in `tests/test_store_database.py`: store the old shape, set `user_version` back to the version before, open the database, and check the setting loads.

## Conventions

- American English, in code, messages and documentation.
- Short commit messages: a subject line, and at most a few lines on why.
- Secrets live in the secrets file (`store/secrets.py`), never in settings. Their values never appear in a repr, a log line or an error message; logs name a secret only.
- A change that adds a translatable message updates the catalogs and both translations in the same commit, or the tests fail.

## More

- [plugins.md](plugins.md): the plugin protocol, and writing a plugin
- [api.md](api.md): the HTTP API
- [mqtt.md](mqtt.md): the MQTT topics and Home Assistant discovery
- [gateway-protocol.md](gateway-protocol.md): the Thermaestro gateway protocol and the Nibe bus
- [probe.md](probe.md): the read-only probe
- [docker.md](docker.md): running it in Docker
