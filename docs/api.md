# The HTTP API

Thermaestro's web UI and its JSON API run in the same server, over the same operations. Everything a page can change, the API can change too, under the same name and with the same rights; a test checks this.

## Where it is

- The API is under `/api/v1/`; paths below are relative to it.
- The server listens on port 8080 (HTTP) and, unless HTTPS is turned off, on 8443 (HTTPS, with the installation's own self-signed certificate). Both serve the same API. The ports and the address are set in the start-up file's `web` section (`listen`, `port`, `https`, `https_port`).
- `GET /api/v1/openapi.json` gives the OpenAPI description to anyone logged in. It also lists the HTML pages' routes. There are no built-in docs pages.
- `GET /health` answers `{"status": "ok"}` without a login.

## Logging in

There are two ways: a bearer token, or a session cookie.

### Tokens

Scripts, the command line and other programs use a token:

```
Authorization: Bearer thm_...
```

- A token belongs to a user and carries a list of rights. It can only carry rights the user has, and it acts with the rights that both the user and the token have.
- It is made on the account page or with `POST /api/v1/tokens`, and shown only once.
- It lasts 365 days unless another lifetime, 1 to 3650 days, is given.
- It stops working when it expires, is revoked, or its user is disabled or removed.
- A request with a token needs no CSRF token. Its cookies are ignored: a request with an `Authorization` header is never also taken as a session, and a header that isn't a valid bearer token means not logged in.

`thermaestro status --url http://HOST:8080` reads `/api/v1/status` with the token in the `THERMAESTRO_TOKEN` environment variable.

### Sessions

`POST /api/v1/login` with `{"name": ..., "password": ...}` sets the `thermaestro_session` cookie (HttpOnly, SameSite=Lax, Secure over HTTPS) and answers `{"csrf": "..."}`.

- A session ends after 12 hours unused, or 7 days after login at most.
- Every unsafe request (anything but GET, HEAD and OPTIONS) with a session must:
  - send the CSRF token from the login answer, in the `X-CSRF-Token` header (a form may send it in the `csrf` field instead);
  - send an `Origin` header, or failing that a `Referer`, naming the host the request was sent to.
- `POST /api/v1/login` and `POST /api/v1/setup` take JSON only. If they carry an `Origin` or `Referer`, it must be this site's.
- Logins are limited to 30 attempts per client address in 5 minutes. After a wrong password, the user's next login is delayed: 1 second, doubling with each further failure, up to 15 minutes. After 100 failures in a row, the user is disabled. Setting a new password enables it again: another administrator can do it under **Users** (or `PUT /api/v1/users/{name}/password`), and on the host `thermaestro admin reset-password <name>` does it for any user, the last administrator included (in Docker: `docker compose exec thermaestro thermaestro admin reset-password <name>`).

### Entering the password again

Changes under `users.manage`, `secrets.manage` and `plugins.manage` need the password entered in the last 15 minutes, and so do creating a token and changing one's own password. A session that logged in longer ago gets `403` with `"confirm": true`; `POST /api/v1/confirm` with `{"password": ...}` renews it. Tokens don't need this.

## Rights

A user has the rights of its groups, plus its own. A new installation has three groups:

| Group | Rights |
|---|---|
| Administrators | `*` (every right) |
| Household | `points.read`, `intent.temporary.create`, `intent.temporary.create.away`, `intent.temporary.create.guests`, `plan.read` |
| Viewers | `points.read` |

`GET /api/v1/rights` lists every right with what it allows. The rights used by the endpoints below:

| Right | Allows |
|---|---|
| `points.read` | see devices, values and their history |
| `settings.read` | see the installation's settings |
| `settings.write` | change the installation's settings |
| `secrets.manage` | enter or replace secrets (never read them back) |
| `plugins.manage` | add, change and remove plugin instances |
| `users.manage` | add, change and remove users and their rights |
| `tokens.own` | create and revoke one's own API tokens |
| `audit.read` | read the audit log |

The `intent.*`, `plan.read` and `levers.control` rights exist, but no endpoint uses them yet.

## Answers and errors

Answers are JSON. Times are seconds since the epoch (`t`, `created`, `expires`), except where an ISO 8601 string is shown. Errors look like this:

```json
{"error": "not allowed: needs settings.write"}
```

| Status | When |
|---|---|
| 400 | The request was refused: a wrong value, a wrong password, an unknown id. The `error` says why. |
| 401 | Not logged in, or the token isn't valid. |
| 403 | A missing right (`not allowed: needs ...`); the password must be entered again (`"confirm": true`); or a missing or wrong CSRF token, or another site's origin. |
| 404 | Not found, where an endpoint says so: a user, a register map. |
| 422 | The body or a query parameter doesn't match the endpoint's typed fields (FastAPI's own format, with `detail`). |

Many endpoints that change a setting take a free JSON object and check it themselves; a wrong one gets `400`, with the field named in `error`.

## Endpoints

The right in each table is what the endpoint requires; "logged in" means any user or token. A ⚿ marks the changes that need the password entered again in a session.

### Logging in and setup

| Method and path | Right | Body → answer |
|---|---|---|
| `POST /login` | none | `{name, password}` → `{csrf}`, and the session cookie |
| `POST /logout` | logged in | → `{status: "ok"}`; ends the session, if there is one |
| `POST /setup` | none | `{code, name, password}` → `{user}`: the first administrator, with the setup code. Works only while no user can manage users. |
| `POST /confirm` | logged in | `{password}` → `{status: "ok"}`; does nothing for a token |

The setup code is written to a file in the state directory, which only the service user can read, at every start until an administrator exists. It lasts 60 minutes and works once. `thermaestro setup-code` prints it, or a new one.

### Values and history

| Method and path | Right | Answer |
|---|---|---|
| `GET /status` | `points.read` | Every plugin instance: `id`, `plugin`, `state`, `failures`, `last_error`, `health`, and `points`, each with `path`, `label`, `category`, `unit`, `digits`, `value`, `quality`, `why`, `t` |
| `GET /history/{instance}/{point}` | `points.read` | `[{t, value, text, quality}]`. Query: `start`, `end` (epoch seconds; default the last 24 hours), up to 31 days. `point` is the point's path, slashes included. |
| `GET /daily/{instance}/{point}` | `points.read` | `[{day, min, mean, max}]`: each day's lowest, mean and highest good value, in the house's time zone. Query: `days`, 1 to 400, default 30. |
| `GET /site` | `points.read` | `{rooms, outdoor, pinned}`: rooms and the outdoors as derived from the sensors |
| `GET /series` | `points.read` | Every series the plugins offer (prices, forecasts), and whether it holds what it should by now |

### Settings and plugins

| Method and path | Right | Body → answer |
|---|---|---|
| `GET /settings/location` | `settings.read` | `{latitude, longitude, timezone}`, or `null` |
| `PUT /settings/location` | `settings.write` | `{latitude, longitude, timezone}` (an IANA zone, such as `Europe/Stockholm`) → the same. The weather providers move with it. |
| `GET /plugins` | `settings.read` | `{id: {plugin, enabled, settings}}` for every plugin instance |
| `PUT /plugins/{id}/pump` | `plugins.manage` ⚿ | A Nibe pump's connection → the instance's setting; (re)starts it. Fields: `host`; `protocol` (`nibegw`, `thermaestro-gw` or `modbus-tcp`); `read_port` (9999), `write_port` (10000), `control_port` (10090), `modbus_port` (502), `local_port` (0: any free port); `psk` (a secret's name, needed for `thermaestro-gw`); `model` (needed for `modbus-tcp`); `brine_flow`, `brine_flow_at`, `brine_mix`. |
| `GET /secrets` | `settings.read` | The names of the secrets entered; never their values |
| `PUT /secrets/{name}` | `secrets.manage` ⚿ | `{value}` → `{status: "ok"}` |
| `GET /nibe/logset?model=F1245` | `settings.read` | A `LOG.SET` file for a bus-family Nibe pump's USB logging; `404` for a model without a register map |
| `GET /names` | `points.read` | `{"<instance>:<path>": name}`: the household's own names for points and nodes |
| `PUT /names` | `settings.write` | `{ref, name}`; `ref` is `<instance>:<path>`; an empty or `null` name brings back the built-in one |
| `GET /display` | `points.read` | `{categories, pinned}`: points moved to another category, and those pinned to the overview |
| `PUT /display` | `settings.write` | `{ref, category, pinned}`; `category` is `primary`, `config`, `diagnostic` or `null` (as the plugin says); `pinned` `null` leaves it unchanged |

### Sensors and rooms

| Method and path | Right | Body → answer |
|---|---|---|
| `GET /sensors` | `settings.read` and `points.read` | `{id: {...sensor, reading}}`; `reading` is `{value, digits, quality, why, t}` or `null` |
| `POST /sensors` | `settings.write` | A sensor → `201`, `{id, ...}`; the id is made from its name |
| `PUT /sensors/{id}` | `settings.write` | A sensor → `{id, ...}` |
| `DELETE /sensors/{id}` | `settings.write` | → `{status: "ok"}`; also removed as an outdoor reference |
| `GET /rooms` | `settings.read` | `{id: {name, climate_system, own_device}}` |
| `POST /rooms` | `settings.write` | A room → `201`, `{id, ...}`; the id is made from its name |
| `PUT /rooms/{id}` | `settings.write` | A room → `{id, ...}` |
| `DELETE /rooms/{id}` | `settings.write` | → `{status: "ok"}`; its sensors stay, without a room |
| `GET /outdoor` | `settings.read` | `{quantity: sensor_id}` |
| `PUT /outdoor` | `settings.write` | `{"temperature": "north-wall"}` → the same. Without one for temperature, the pump's outdoor sensor is the reference. |

A sensor:

| Field | |
|---|---|
| `name` | required |
| `source` | `mqtt` or `point` |
| `topic`, `json_key` | for `mqtt`: the topic, and for a JSON payload the field with the value (dotted for a nested one); without `json_key` the payload is the number |
| `point` | for `point`: `<instance>:<path>`, such as `ha:sensor.bedroom_temperature` |
| `quantity` | default `temperature`: a Home Assistant device class, or a room point |
| `placement` | `room` (default), `outdoor` or `other` |
| `room`, `reference` | for a room sensor: its room's id, and whether it alone stands for the room |
| `freshness_s` | how long it may stay quiet before its value is stale; `null` learns it from how often it reports |
| `calibration_offset` | added to each value, default 0 |

A room: `name`, `climate_system` (the climate system's node, such as `pump:hp1/cs1`), and `own_device`: `unknown` (default), `none`, `simple_thermostat`, `smart_thermostat`, `radiator_valves` or `zone_controller`.

### MQTT, Home Assistant discovery and Home Assistant

| Method and path | Right | Body → answer |
|---|---|---|
| `GET /mqtt` | `settings.read` | `{settings, state, error}` |
| `PUT /mqtt` | `settings.write` | `{host, port, username, password, tls, enabled}` → the settings. `password` is a secret's name; enter the secret first with `PUT /secrets/mqtt.password`. |
| `GET /discovery` | `settings.read` | `{settings, state, devices}`: the setting, and what is published now |
| `PUT /discovery` | `settings.write` | `enabled`, `prefix` (default `homeassistant`), `base` (default `thermaestro`), `sensors`, `language` (`en`, `sv`, `de`) → the settings. The installation's `id` is made when discovery is first switched on. |
| `PUT /homeassistant/{id}` | `plugins.manage` ⚿, and `secrets.manage` ⚿ with a token | `{url, token}` → the instance's setting. `token` is a new long-lived access token, kept as the secret `<id>.token`; `null` keeps the one entered. The entities chosen so far are kept. |
| `GET /homeassistant/{id}/entities` | `settings.read` | The entities worth reading: `entity_id`, `name`, `domain`, `device_class`, `unit`, `area`, `quantity`, `points` |
| `POST /homeassistant/{id}/sensors` | `settings.write` and `plugins.manage` ⚿ | `[{entity, point, quantity, name, room, new_room, placement}]` → `201`, `{sensors: [ids]}`. `new_room` instead of `room` names a room to make, or to use where one has that name. |
| `GET /homeassistant/{id}/areas` | `settings.read` | The areas of Home Assistant's entities that aren't a room yet |
| `POST /homeassistant/{id}/rooms` | `settings.write` | `{areas: [names]}` → `201`, `{rooms: [ids]}`: a room of each area. Once made, a room doesn't follow changes in Home Assistant. |

### Prices

| Method and path | Right | Body → answer |
|---|---|---|
| `GET /prices?day=YYYY-MM-DD` | `points.read` | What one more kWh costs in each 15-minute slot of a day (default today): `{day, unit, problems, warnings, slots, checks}`. Each slot has `start`, `end`, `total`, `missing` and `parts` (`layer`, `role`, `value`, `vat_added`, `fallback`, `carried_from`). `checks` compares the total with a supplier's own, where one is offered. |
| `GET /prices/sources` | `settings.read` | `{id: {plugin, settings, state, needs_user_action, error, area, currency}}` |
| `PUT /prices/sources/{id}` | `plugins.manage` ⚿ | `{plugin, settings}` → the instance's setting; (re)starts it. `plugin` is `tibber`, `entsoe`, `energy_charts`, `nordic_sites`, `omie` or `octopus_agile`. A token in the settings is a secret's name. |
| `GET /prices/spot` | `settings.read` | `{zone, source, fallback}` |
| `PUT /prices/spot` | `plugins.manage` ⚿ | `{zone, source, fallback, currency}` → `{id, ...layer}`: take a bidding zone's spot price from one source, with another standing in. The sources a zone has are those Setup → Prices offers. Sources that need no account are set up for the zone, and removed once unused. |
| `GET /prices/layers` | `settings.read` | `{id: layer}` |
| `POST /prices/layers` | `settings.write` | A layer → `201`, `{id, ...}`; the id is made from its role |
| `PUT /prices/layers/{id}` | `settings.write` | A layer → `{id, ...}` |
| `DELETE /prices/layers/{id}` | `settings.write` | → `{status: "ok"}`; also removed from the VAT rule |
| `GET /prices/vat` | `settings.read` | `{rate, applies_to}`, or `null` |
| `PUT /prices/vat` | `settings.write` | `{rate, applies_to}`: the rate (`0.25` for 25 %) and the layers it is charged on, by id or role |

A layer: `role` (such as `spot` or `grid.fee`), `source` (`series` or `fixed`), `unit`, `vat` (`incl` or `excl`). A `series` layer names its `plugin` instance and `series`, and may list `fallbacks` as `<instance>:<series>`. A `fixed` layer gives its `value`.

### Weather

| Method and path | Right | Body → answer |
|---|---|---|
| `GET /weather` | `points.read` and `settings.read` | `{providers, choice}`: each provider with its terms, and for each quantity whether it gives it, derives it or lacks it |
| `PUT /weather/choice` | `settings.write` | `{"main": "met", "quantities": {"irradiance.global": "open-meteo"}, "fallbacks": ["homeassistant:weather.forecast_home"]}` → the same |
| `GET /weather/forecast?hours=48` | `points.read` | Per quantity: `quantity`, `unit`, `source`, `derived`, `fallback`, `stale`, and `values` (`start`, `end`, `value`) from this hour on; `hours` 1 to 240 |
| `GET /weather/scores` | `points.read` | How each provider has done at this house over the last 30 days: `source`, `quantity`, `lead_h`, `n`, `bias`, `mae`, `enough` |
| `GET /weather/sources` | `settings.read` | `{id: {plugin, settings, state, needs_user_action, error}}` |
| `PUT /weather/sources/{id}` | `plugins.manage` ⚿ | `{"plugin": "met_norway"}`, `"smhi"`, or `{"plugin": "open_meteo", "model": "icon_seamless"}` → the instance's setting. It forecasts for the location. |
| `DELETE /weather/sources/{id}` | `plugins.manage` ⚿ | → `{status: "ok"}` |
| `PUT /weather/homeassistant/{id}` | `plugins.manage` ⚿ | `{"entity": "weather.forecast_home"}` → the Home Assistant instance's setting: read that weather entity's forecast |
| `GET /weather/climate` | `settings.read` | `{annual_mean, monthly_spread, monthly_means, source, period}`, or `null` |
| `PUT /weather/climate` | `settings.write` | `{"annual_mean": 7.5, "monthly_spread": 20.0}`, in °C and K → the climate |
| `POST /weather/climate/fetch` | `settings.write` | → the climate from Open-Meteo's archive: the last ten whole years at the location |

### Users, groups, tokens and sessions

| Method and path | Right | Body → answer |
|---|---|---|
| `GET /users` | `users.manage` | `[{name, groups, rights, disabled}]` |
| `POST /users` | `users.manage` ⚿ | `{name, password, groups}` → `201`, `{user}` |
| `PUT /users/{name}/groups` | `users.manage` ⚿ | `{groups}` → `{status: "ok"}` |
| `PUT /users/{name}/password` | `users.manage` ⚿; one's own: ⚿ only | `{password, end_sessions}` (`end_sessions` defaults to `true`) → `{status: "ok"}` |
| `DELETE /users/{name}` | `users.manage` ⚿ | → `{status: "ok"}` |
| `GET /groups` | `users.manage` | `{group: [rights]}` |
| `PUT /groups/{name}` | `users.manage` ⚿ | `{permissions}` → `{status: "ok"}`; makes the group if it is new |
| `DELETE /groups/{name}` | `users.manage` ⚿ | → `{status: "ok"}` |
| `GET /rights` | logged in | `{right: what it allows}` |
| `GET /account/preferences` | logged in | One's own language and formats |
| `PUT /account/preferences` | logged in | `language` (`en`, `sv`, `de`), `region` (a country code, such as `SE`), `dates` (`iso`), `clock` (`24`, `12`), `decimal` (`point`, `comma`) → the same. A field left out follows the browser. |
| `GET /tokens` | `tokens.own` | One's own tokens: `[{id, name, permissions, created, expires, last_used}]` |
| `POST /tokens` | `tokens.own` ⚿ | `{name, permissions, days}` → `201`, `{token}`, shown this once |
| `DELETE /tokens/{token_id}` | `tokens.own`; another user's also `users.manage` | → `{status: "ok"}` |
| `GET /sessions?user=NAME` | logged in; another user's: `users.manage` | `[{id, created, last_seen, from, agent}]`; without `user`, one's own |
| `DELETE /sessions/{user}/{session_id}` | logged in; another user's: `users.manage` | → `{status: "ok"}` |

The system keeps at least one enabled user who can manage users: a change that would leave none is refused.

### The audit log

| Method and path | Right | Answer |
|---|---|---|
| `GET /audit?limit=200` | `audit.read` | The newest entries first, 1 to 2000: `{t, who, from, what, why, outcome, details, prev}`. `t` is an ISO 8601 time; `prev` is the hash of the entry before, which chains the log. |

Every change through the API is in the audit log, with who made it and from which address, except one's own language and formats. Secrets' names may be in it, their values never.

## Not in the API yet

The household's intents, the plan and the control levers have no endpoints yet.
