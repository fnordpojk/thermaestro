# Replaying a recording

A period of a running installation can be exported and the learners run over it again, day by day, as they would have run then: to see what they make of real data, to score the hot-water draws they find against what the household noted, and to check a change to a learner against weeks already recorded.

## Exporting

On the machine Thermaestro runs on, from its state directory's database (in Docker: `docker compose exec thermaestro thermaestro export /data/week.jsonl.gz`, then `docker compose cp thermaestro:/data/week.jsonl.gz .`):

```sh
thermaestro export week.jsonl.gz --days 14
```

`--days` is how far back from now; the raw history is kept 14 days, its 15-minute aggregates far longer, but the learners need the raw readings. The file is gzip-compressed JSON lines: a header, then one record a line. It holds:

- what each plugin described of its devices (a unit's serial number left out);
- the settings learning reads: the house's answers, rooms, sensors, the outdoor references, names, control, the documented figures, the climate and the weather choice; of the location, only its time zone and hemisphere;
- the history in the period, raw and as 15-minute aggregates;
- the forecasts' and prices' intervals, and the forecasts kept at lead times with what was measured then. Where a provider gives cloud cover but no sunlight, the sunlight worked out from it is in the file, since the coordinates aren't;
- what was decided (in shadow, what would have been), with what the device showed;
- intents, levels, what had been learned, and how the inputs were judged.

**Not in it:** accounts, sessions, API tokens, the audit log, secrets, and plugins' connection settings (hosts, ports, keys). Room, sensor and device names and MQTT topics are. Look through a file before sending it to anyone.

## Replaying

```sh
thermaestro replay week.jsonl.gz replayed/ --labels hot-water.csv
```

The replay loads the export into a new database in `replayed/` and steps through it a day at a time: at each day's end, an hour on, it adds what had been recorded by then, judges the inputs and runs every learner with the clock at that moment. Nothing learns from what came after, so replaying fewer days (`--days 3`) gives the same values for those days. It needs no plugin and touches no device.

What it writes to `replayed/`:

| file | what |
|---|---|
| `audit.json` | the export's SHA-256 and header, the days replayed, each point's coverage (first and last sample, how many, the hours between), and the scorecard's totals |
| `learned.jsonl` | every learned value after each day: its state, value, what is used, and why |
| `learned.json` | what was learned at the end, beside what the installation had learned when it was exported |
| `inputs.json` | how each input was judged at the end |
| `scorecard.json` | with `--labels`: each label, found or not, and the draw it matched |
| `replay.db` | the replay's database, to look into |

## Labels for hot-water use

A CSV file with a header, `time,what` and, where known, `liters`; the time in the house's time zone:

```csv
time,what,liters
2026-10-14 07:15,shower,
2026-10-14 19:30,bath,120
2026-10-15 08:05,dishwasher,
```

A label is found where a draw started or peaked within an hour of it, each draw matching one label at most, the nearest first. The scorecard counts what was found, what was missed, and the draws no label explains: small draws nobody noted end up there, so the count of found and missed says more.
