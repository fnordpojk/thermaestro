# The read-only probe

`thermaestro probe` reads a Nibe bus-family pump (F-series, VVM, SMO) through its NibeGW gateway and writes two files: a report, and a capture of the bus. Send them with an issue about a model Thermaestro doesn't know yet, or with a bug.

It **writes nothing to the pump**. It reaches the pump only through a transport that has no way to send a write.

## Running it

```
thermaestro probe 192.0.2.10
```

The address is the gateway's (esphome-nibe, or another NibeGW). Options:

- `--protocol thermaestro-gw --key-file FILE`: use the Thermaestro gateway protocol, with the gateway's 64-digit key in `FILE` (not on the command line, where other users of the host could see it);
- `--read-port`, `--control-port`: if the gateway uses other ports than 9999 and 10090;
- `--local-port`: for a gateway that sends to a fixed port;
- `--model F1245`: if the pump's own product name isn't recognized;
- `--minutes 5`: how long to capture the bus;
- `--out DIR`: where to write the files.

It takes about the minutes given, and longer if some values haven't been read by then (up to two more minutes): values the pump doesn't send by itself are read about one a second.

## What the files hold

- **`probe-<model>-<time>.json`**, the report:
  - the pump's product name, model, firmware and word order;
  - which extra climate systems the model can have, and what detection found;
  - each of Thermaestro's points that the model's register map has, with its value and quality;
  - those it lacks, and which control levers Thermaestro could offer;
  - which registers the pump sends by itself, and how many bus exchanges of each kind were seen.

  It holds no network address, key or personal name.
- **`probe-<model>-<time>-capture.jsonl`**, the capture: every bus exchange the gateway forwarded, one per line, with its time. It holds everything on the bus, other devices' traffic too.

Look through both files before sending them.
