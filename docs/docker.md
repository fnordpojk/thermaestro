# Running Thermaestro with Docker Compose

`compose.yaml` in this repository runs Thermaestro's core in Docker, on a 64-bit Linux machine (amd64 or arm64). Docker builds the image on that machine, from this repository on GitHub; there are no ready-made images yet.

## What the machine needs

- Docker Engine with the Compose plugin (`docker compose`, not the old `docker-compose`).
- git, which Docker uses to fetch the repository when it builds.
- To reach the pump's gateway, or an S-series pump, on the local network.

## Bringing it up

1. Put `compose.yaml` in a directory of its own, say `~/thermaestro`, and in that directory run:

   ```
   docker compose up -d
   ```

   The first build takes a few minutes.
2. Create the first administrator. Until there is one, Thermaestro logs a setup code at each start:

   ```
   docker compose logs thermaestro
   ```

   shows "No administrator yet. Create one in the web UI with the setup code …". `docker compose exec thermaestro thermaestro setup-code` shows it again, or a new one.
3. Open `http://<the machine's address>:8080`, and enter the setup code, a user name and a password of at least 15 characters.
4. HTTPS is on port 8443. The browser warns about the certificate, which is the installation's own. Its SHA-256 fingerprint is under System → Health, to compare with the one the browser shows.

## Updating

```
docker compose up -d --build
```

fetches the main branch again, builds, and restarts Thermaestro if anything changed. Its data stays.

## Settings in the compose file

These can go in a file named `.env` beside `compose.yaml`, such as `THERMAESTRO_HTTP_PORT=8081`:

| variable | default | |
|---|---|---|
| `THERMAESTRO_HTTP_PORT` | 8080 | the machine's port for the web UI |
| `THERMAESTRO_HTTPS_PORT` | 8443 | the same, over HTTPS |
| `THERMAESTRO_SOURCE` | `https://github.com/fnordpojk/thermaestro.git#main` | what is built: another branch (`…/thermaestro.git#<branch>`), or a local clone (`.`, with `compose.yaml` in the clone) |

Change the machine's ports here rather than Thermaestro's own in the start-up file: the image's health check expects 8080 inside the container.

## Where things are

- **`./config`**, beside `compose.yaml`: the optional start-up file `thermaestro.toml` (history lengths and the like). It is mounted read-only; without the file, everything has its default.
- **The `thermaestro_data` volume**: the database, the secrets, the HTTPS certificate and the audit log. This is the same state directory as `/var/lib/thermaestro` on a Raspberry Pi, so moving an installation between the two is a copy of it.

To back it up, stop Thermaestro so the database is at rest, copy the volume's contents out, and start it again:

```
docker compose stop
docker compose cp thermaestro:/data ./thermaestro-backup
docker compose start
```

## Moving an existing installation in

An installation's state directory, from a Raspberry Pi (`/var/lib/thermaestro`) or another Docker host, or one run from a clone, goes into the volume before Thermaestro's first start there.

1. Stop the old installation, and leave it stopped: two running copies would publish the same Home Assistant topics. On the old machine, pack its state directory, readable only by you, since it holds the secrets:

   ```
   umask 077
   tar -C <the state directory> -czf thermaestro-state.tar.gz .
   ```

2. Copy the file beside `compose.yaml` on the new machine, and there:

   ```
   docker compose build
   docker compose run --rm -T --no-deps --entrypoint tar thermaestro -xzpf - -C /data < thermaestro-state.tar.gz
   docker compose up -d
   rm thermaestro-state.tar.gz
   ```

   The files are unpacked inside the container, as its own user, so they are that user's from the start, as Thermaestro requires. (`docker compose cp` would make them root's.)

After the move:
- **Addresses ending in `.local`**, such as `homeassistant.local`, don't resolve inside the container. Use an IP address or a name your DNS knows.
- **Everyone logs in again** at the new address; users and passwords came along.
- **The database is brought up to the new version** at the first start, so the old installation, if older, can't use it again.

## Removing it

`docker compose down` removes the container and keeps the volume; `docker compose down -v` deletes the volume too, and with it everything Thermaestro knew.

## The pump

The container uses Docker's own network, and reaches the network outside through the machine's address.

- **esphome-nibe** (plain NibeGW, or the Thermaestro gateway protocol) answers where Thermaestro sends from, and an **S-series pump's Modbus TCP** is a connection Thermaestro opens. Neither needs anything in `compose.yaml`.
- **A gateway that sends to a fixed address and port**, such as openHAB's NibeGW: point it at the machine's address, set that port as the local port under Setup → Pump, and publish it in `compose.yaml` (the commented-out `9999:9999/udp` line, with the port changed to match).

## How it runs

- As its own user (UID and GID 10001), not root.
- On a read-only file system, apart from `/data` and a `/tmp` in memory.
- With no Linux capabilities, and no way to gain privileges.
- Logging to Docker, kept to 100 MB.
