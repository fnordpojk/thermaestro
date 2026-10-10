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
- A request with the header `X-Thermaestro-Background: 1` doesn't count as use: the web UI's pages send it when they refresh what they show by themselves, so a page left open still logs out after 12 hours.
- Every unsafe request (anything but GET, HEAD and OPTIONS) with a session must:
  - send the CSRF token from the login answer, in the `X-CSRF-Token` header (a form may send it in the `csrf` field instead);
  - send an `Origin` header, or failing that a `Referer`, naming the host the request was sent to.
- `POST /api/v1/login` and `POST /api/v1/setup` take JSON only. If they carry an `Origin` or `Referer`, it must be this site's.
- Logins are limited to 30 attempts per client address in 5 minutes. After a wrong password, the user's next login is delayed: 1 second, doubling with each further failure, up to 15 minutes. After 100 failures in a row, the user is disabled. Setting a new password enables it again: another administrator can do it under **Users** (or `PUT /api/v1/users/{name}/password`), and on the host `thermaestro admin reset-password <name>` does it for any user, the last administrator included (in Docker: `docker compose exec thermaestro thermaestro admin reset-password <name>`).

### Wall displays

A wall display is a browser that stays logged in, such as a tablet on the wall showing the overview:

- It is made on the account page or with `POST /api/v1/displays`, by a user with `wall_displays.own`, with a name and a list of rights, which can only be rights the user has. The answer is its link, `https://HOST/wall-display/thd_...`, shown only once.
- The link is opened in the display's browser within a day, once. Its page asks before opening, so a chat program's or mail scanner's preview of the link doesn't use it up. `POST /api/v1/displays/open` with `{"link": ...}` does the same for a program, and answers `{"csrf": "..."}` like a login.
- The display's session never idles out or expires. Its cookie is set again with each page it loads, so a browser's own limit on cookies (400 days) doesn't end it either. It ends when the display is revoked, its user's password changes, or its user is disabled or removed.
- It acts with the rights that both the user and the display have, and can't make changes that need the password entered again. The audit log names it `display:<user>`.

### Entering the password again

Changes under `users.manage`, `secrets.manage` and `plugins.manage` need the password entered in the last 15 minutes, and so do creating a token or a wall display and changing one's own password. A session that logged in longer ago gets `403` with `"confirm": true`; `POST /api/v1/confirm` with `{"password": ...}` renews it. Tokens don't need this.

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
| `wall_displays.own` | make and revoke one's own wall displays, which stay logged in |
| `audit.read` | read the audit log |
| `intent.temporary.create` | ask for something for a while: warmer, a bath, a boost, a fireplace |
| `intent.temporary.create.away`, `.guests` | say the house is away, or has guests, until a date |
| `intent.handsoff.create` | stop Thermaestro changing the pump for up to 48 hours |
| `intent.standing.write` | set what the household always wants: bands, hot water, limits |
| `intent.levels.write` | add, change and remove levels |
| `intent.ranking.write`, `intent.slider.write` | change what gives way first, and how much comfort may give for savings |
| `intent.any.end` | end what someone else asked for |
| `plan.read` | see the intents, the levels and the plan, and why |
| `levers.control` | put levers off, in shadow or in control ⚿ |

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
| `PUT /prices/sources/{id}` | `plugins.manage` ⚿ | `{plugin, settings}` → the instance's setting; (re)starts it. `plugin` is `tibber`, `entsoe`, `energy_charts`, `nordic_sites`, `omie`, `octopus_agile` or `energidataservice`. A token in the settings is a secret's name. |
| `GET /prices/spot` | `settings.read` | `{zone, source, fallback}` |
| `PUT /prices/spot` | `plugins.manage` ⚿ | `{zone, source, fallback, currency}` → `{id, ...layer}`: take a bidding zone's spot price from one source, with another standing in. The sources a zone has are those Setup → Prices offers. Sources that need no account are set up for the zone, and removed once unused. |
| `GET /prices/layers` | `settings.read` | `{id: layer}` |
| `POST /prices/layers` | `settings.write` | A layer → `201`, `{id, ...}`; the id is made from its role |
| `PUT /prices/layers/{id}` | `settings.write` | A layer → `{id, ...}` |
| `DELETE /prices/layers/{id}` | `settings.write` | → `{status: "ok"}`; also removed from the VAT rule |
| `GET /prices/vat` | `settings.read` | `{rate, applies_to}`, or `null` |
| `PUT /prices/vat` | `settings.write` | `{rate, applies_to}`: the rate (`0.25` for 25 %) and the layers it is charged on, by id or role |
| `GET /prices/grid-rules` | `settings.read` | `{id: rule}`: the grid company's rules entered |
| `POST /prices/grid-rules` | `settings.write` | A rule → `201`, `{id, ...}`, the id made from the grid company's name. `type` is `tou` (a time-of-use price per kWh: `base`, and `rates`, each with `months`, `days`, `start`, `end` and `price`; the first that holds applies), `interval_peak` (a power charge: `window`, `interval_minutes`, `peaks`, `different_days`, `price_per_kw`) or `subscribed_power` (`kw`). Each has `owner`, `status` (`in_force`, `announced`, `paused`, `withdrawn`), `valid_from`, `valid_to`, `clock` (`civil`, or `normal` time all year), `unit`, `vat`, `note`, and `unknown`: the fields the grid company hasn't given. `days` is `all`, `working_days` (Monday to Friday except public holidays), `non_working_days`, `weekdays` or `weekends`. A time-of-use rule becomes a layer of the stack, role `grid.tou`. |
| `PUT /prices/grid-rules/{id}` | `settings.write` | A rule → `{id, ...}` |
| `DELETE /prices/grid-rules/{id}` | `settings.write` | → `{status: "ok"}`; its layer goes too |
| `GET /prices/denmark/companies` | `settings.read` | Denmark's grid companies with a household tariff today, from Energi Data Service: `[{gln, company, tariff, note, codes}]`, `codes` being the tariff's and any rebate's |
| `PUT /prices/denmark` | `plugins.manage` ⚿ | `{gln, tariff}` from that list → `{added}`: an `energidataservice` source offering `grid` (`grid.tou`), `energinet` (`grid.transfer`) and `elafgift` (`tax.energy`), in DKK/kWh without VAT, each added as a layer where no layer has its role |

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
| `GET /sessions?user=NAME` | logged in; another user's: `users.manage` | `[{id, created, last_seen, from, agent, display}]` (`display`: a wall display's name); without `user`, one's own |
| `DELETE /sessions/{user}/{session_id}` | logged in; another user's: `users.manage` | → `{status: "ok"}` |
| `GET /displays` | `wall_displays.own` | One's own wall displays: `[{id, name, permissions, created, link_expires, opened, last_seen}]` |
| `POST /displays` | `wall_displays.own` ⚿ | `{name, permissions}` → `201`, `{link}`, shown this once |
| `DELETE /displays/{display_id}` | `wall_displays.own`; another user's also `users.manage` | → `{status: "ok"}`; its browser is logged out |
| `POST /displays/open` | anyone, with the link | `{link}` → `{csrf}`, and the session's cookie |

The system keeps at least one enabled user who can manage users: a change that would leave none is refused.

### The audit log

| Method and path | Right | Answer |
|---|---|---|
| `GET /audit?limit=200` | `audit.read` | The newest entries first, 1 to 2000: `{t, who, from, what, why, outcome, details, prev}`. `t` is an ISO 8601 time; `prev` is the hash of the entry before, which chains the log. |

Every change through the API is in the audit log, with who made it and from which address, except one's own language and formats. Secrets' names may be in it, their values never.

### Intents and levels

What the household wants. A request is in household terms, one `kind` with only its own fields; the right it needs comes from the kind (above).

| Method and path | Right | Body → answer |
|---|---|---|
| `GET /intents?ended=false` | `plan.read` | The open intents; with `ended=true`, the finished ones too |
| `POST /intents` | the kind's | A request → `201`, `{accepted, intent, messages}`. `messages` say when it ends, what it sets aside, and what the house can't do. A refused one has `accepted: false` and says why. |
| `PUT /intents/{id}` | as ending it, plus the kind's for the new contents | The whole request, as for `POST /intents` (`kind` may be left out; it can't change) → `{accepted, intent, messages}`. Changed in place: the same id, asker and time asked; checked as a new request would be; an open intent only. A seeded one becomes the household's own. Audited with before and after. |
| `DELETE /intents/{id}` | one's own: the kind's right; anyone's: `intent.any.end` | → the intent, finished |
| `POST /intents/understand` | `plan.read` | `{text}` → `{kind, request, missing}`: what was typed ("a bath at 19:30", "borta till söndag", "Gäste bis morgen"), read as a request for a while, in English, Swedish or German. Nothing is asked: send `request` to `POST /intents` once `missing` (`until`, `by`, `levels`) is filled in. `kind` is `null` when it wasn't understood. |
| `POST /intents/{id}/confirm` | `intent.standing.write` | → a seeded intent, now the household's own |
| `GET /intents/in-force` | `plan.read` | What applies now: `bounds` (`target`, `scope`, `low`, `high`, `value`, the edges' ranks, the intents behind it), `paused` (intent id → why), `hands_off_until`, `ranking`, `slider` |
| `GET /levels` | `plan.read` | `[{id, name, scope, low, high, top}]` |
| `PUT /levels/{id}` | `intent.levels.write` | `{"name": "Day", "scope": "pump:hp1/cs1", "low": 20.5, "high": 22}`, or a tank's `{"name": "Normal", "scope": "pump:hp1/dhw", "top": 50}` |
| `DELETE /levels/{id}` | `intent.levels.write` | Refused while an intent names it |

The requests, each with what it takes besides `kind` (times are ISO 8601 with an offset; `days` 0 Monday to 6 Sunday; spans `{days, start, end}` in local times):

| `kind` | Fields |
|---|---|
| `warmer` | `scope` (a climate system or room), `offset` (°C, negative for cooler), `until` (else the next change of the pattern, at most a day later) |
| `bath` | `scope` (the tank), `at_least` (°C), `by`, `strength` (`must`, the default, or `should`) |
| `guests` | `until`, `levels` (climate system → level), `hot_water` (a tank level) |
| `away` | `until`, `levels` |
| `hands_off` | `until`, at most 48 hours ahead |
| `fireplace`, `boost_now` | `scope` |
| `comfort_band` | `scope`, `pattern` (`[{level, days, start, end}]`), `season` (`["10-01", "04-30"]`); or `no_sensor: true` with `steps` (1 to 5) |
| `hot_water_by` | `scope`, `deadlines` (`[{by, days, level}]` or a `temp` instead of a `level`), `strength` |
| `hot_water_floor` | `scope`, `temp` |
| `cost_stance` | `slider` (0 to 1), `ranking` (an order of `comfort_low`, `must_deadlines`, `should_deadlines`, `comfort_high`, `power_peak`) |
| `addition_policy` | `policy` (`pump`, `when_needed`, `not_when_expensive`, `limit`), `kw` with `limit` |
| `pool` | `scope`, `level`, `spans` |
| `power_peak` | `kw`, `spans`; needs a whole-house power reading |
| `quiet_hours` | `spans` |

### Setup's answers about the house

| Method and path | Right | Body → answer |
|---|---|---|
| `GET /home` | `settings.read` | The answers |
| `PUT /home` | `settings.write` | `{"emitters": {"pump:hp1/cs1": "slab"}, "house": "average", "water": "well", "holidays": "SE", "holidays_as": 6, "past_deadline": "keep_heating"}`. Emitters: `radiators`, `fan_coils`, `floor_light` (underfloor heating in boards or grooves), `slab` (in a concrete slab), `radiators_and_floor_light`, `radiators_and_slab`, `unknown`; house: `poorly_insulated`, `average`, `well_insulated`, `low_energy`, `unknown`; water: `municipal`, `well`, `unknown`. A field left out takes its default. `check_emitters` lists the climate systems whose answer was plain floor heating, taken as a slab, to be checked; answering again clears it. |

### Coming from NibePi

| Method and path | Right | Body → answer |
|---|---|---|
| `POST /import/nibepi` | `settings.write` | `{config}`, the text of NibePi's `config.json` → a draft, kept for this user for half an hour: `{token, line, items, secrets, rows, notes, timezone, localhost_broker, broker_answers}`. `items` are what it would make, each `{key, kind, id, body, what, optional}`; `secrets` the secrets it would keep, by name only; `rows` every key in the file with what became of it (`carried`, `translated`, `left_out`) and why. Nothing is made. |
| `POST /import/nibepi/{token}` | `plugins.manage` ⚿ | `{items, timezone, model}`: the `key`s of the items to make, the location's time zone, and an S-series pump's model when the draft has none → `{done, problems}`. Each is made as setup would make it; one refused doesn't stop the others. |

### Levers and the plan

A lever is a setting Thermaestro could change. Every lever starts off: Thermaestro reads and plans, and changes nothing. In shadow it decides and records what it would do, and sends nothing; in control it sends.

| Method and path | Right | Body → answer |
|---|---|---|
| `GET /levers` | `settings.read` | `[{lever, kind, mode, unavailable, works, range, competing, claimed, baseline, last, held, drift, writes_today, budget}]`: `competing` lists the device's own features that change the same, each with whether it's confirmed off; `baseline` is what it was found at; `drift` why it was let go after a change made elsewhere |
| `PUT /levers/{lever}/mode` | `levers.control` ⚿ | `{"mode": "shadow"}`: `off`, `shadow` or `control`. Leaving control puts the setting back as it was found. |
| `PUT /levers/{lever}/confirmed-off` | `levers.control` ⚿ | `{"features": ["Smart Price Adaption"]}`: these are switched off on the device |
| `POST /levers/{lever}/accept-drift` | `levers.control` ⚿ | Keep the change made elsewhere: the lever is taken over again from how it is now |
| `GET /plan` | `plan.read` | `{at, decisions, notices, ahead, house_kw, limit_kw, limit_why, ranking}`: the planner's last round, each decision (`lever`, `op`, `params`, `rank`, `reason`) with its `outcome`; `notices`, newest first, are what shadow would have done; `ahead`, the hot-water deadlines to come, each with the cheapest time to charge for it (`charge_from`); the house's power and its limit: the household's, or the grid company's where lower, which `limit_why` names |
| `GET /plan/changes?hours=24` | `plan.read` | Every change asked in the last `hours` (at most 168), newest first: `{t, lever, op, params, who, why, mode, outcome, detail}`, made, shadowed or refused |
| `GET /shadow?days=7&lever=&day=` | `plan.read` | What shadow would have done, newest first: `{t, lever, op, params, why, found}`, where `found` is what the device showed at that moment (point → `{value, unit}`: the point the lever is checked by, and those it writes). Over the last `days` (at most 30), or one `day` (`YYYY-MM-DD`, the house's), for one `lever` or all |
| `GET /shadow.csv` | `plan.read` | The same as CSV: `time` (the house's zone), `lever`, `asked`, `value`, `the device showed` (`point=value unit` pairs), `why` |
| `GET /shadow/series?lever=…&days=7` | `plan.read`, `points.read` | For a chart: `pump`, the history of the point the lever is checked by (`{t, value, text, quality}`), and `shadow`, what shadow would have had as steps `{t, value}`: a setting's value; a hold's `held` or `released`; a trigger's `started`, then `null` after 15 minutes |

A lever is written as `<instance>:<path>`, such as `pump:hp1/cs1/heating.offset`.
