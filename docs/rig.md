# The rig: trying a setting on a pump

`thermaestro rig` tries one of the settings Thermaestro changes on a Nibe pump, watches what the pump does, and puts the setting back as it was found. It goes through the same code Thermaestro uses when a setting is in control: the setting is taken over with its value recorded first, the change is checked and read back, and on the way out it is put back. So what it shows is what control will do.

Run it before switching a setting to control on a model or firmware where it hasn't been tried, or to report how a setting behaves on your pump.

It **writes nothing unless you give `--write`**. Without it, every step before a write runs (taking the setting over, the checks, the value it would write) and the pump is only read, through a connection that has no way to send a write. With `--write`, it says what it will write and asks before the first change.

## Running it

```
thermaestro rig offset 192.0.2.10
thermaestro rig block 192.0.2.10 --write
```

The address and the connection options are the probe's ([probe.md](probe.md)): `--protocol`, `--key-file`, `--read-port`, `--write-port` (10000 by default), `--control-port`, `--local-port`, `--model`.

The checks:

| check | what it does |
|---|---|
| `offset` | Sets climate system 1's heating offset one step up (down at +10), reads it back, and puts it back. `--system N` for another climate system. |
| `mode` | Changes the hot-water mode, reads it back, and puts it back. By default it goes one mode lower (Luxury to Normal, Normal to Economy), or from Economy to Normal; `--mode eco`, `normal` or `lux` names one. |
| `block` | Holds off the pump's own hot-water charges by lowering the current mode's start temperature to 25 °C. It waits until the charge sensor falls below the real start temperature, where the pump would have started a charge, and watches 15 minutes more for one. Then it puts the start temperature back and watches for the charge that should follow. |
| `boost` | Starts one extra charge of hot water and watches it until it ends. |
| `addition` | Waits for the electric addition to run, then sets its stop temperature as low as it goes, which keeps it off, and watches it stop. Then it puts the setting back. `--setting max_power` tries the addition's most power instead, set to 0. |
| `pool` | A pool's start and stop temperatures, each changed and put back, and its heating switched off while it heats. `--pool 2` for the second pool. |
| `restore` | Puts back what a run that was stopped left changed (see below). |

`--minutes` (240 by default) is the longest a check waits for the pump to do something by itself: the tank to cool, the addition to start. Ctrl-C ends a check at any time and puts everything back.

Before the first write it asks whether the pump's own features that change the same settings are switched off: the hot-water schedule (menu 2.3), and on pumps that have it, Smart Price Adaption. A setting whose features aren't confirmed off isn't changed. Where a person has to judge, it asks "Did the pump do what you expected?". Answer `y` or `n`.

If Thermaestro itself runs against the same pump, keep the setting off or in shadow there while the rig runs. If another program changes the setting during a check (NibePi, the pump's menu), the rig lets it go and doesn't write over it, and the table says so.

**In Docker**, run it inside the container and keep its files on the data volume:

```
docker compose exec thermaestro thermaestro rig block 192.0.2.10 --write --out /data/rig
```

## What it writes down

The check ends with a table of its steps: what was asked or seen, what came of it, and `ok`, `FAILED` or `-` where there is nothing to judge. When everything went as it should on a run that wrote, it says so: that is evidence the setting works on this model and firmware.

`--out DIR` (the current directory by default) gets:

- **`rig-<check>-<model>-<time>.json`**, the log: the pump's model and firmware, the steps with their times, your answers, and the values watched, once a minute while it waits. It holds no network address or key, so it can be sent with an issue.
- **`rig-state/`**: what the rig has taken over, kept while it runs. If the rig is killed before it puts things back (a power cut, `kill -9`), no check starts until `thermaestro rig restore ADDRESS --out DIR --write` has put them back, with the same `DIR`. Without `--write`, `restore` only says what it would put back.
