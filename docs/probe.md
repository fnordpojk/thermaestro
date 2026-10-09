# The read-only probe

`thermaestro probe` reads a Nibe pump and writes two files: a report, and a capture of the traffic. It reads a bus-family pump (F-series, VVM, SMO) through its NibeGW gateway, and an S-series pump over its own Modbus TCP. Send the files with an issue about a model Thermaestro doesn't know yet, or with a bug.

It **writes nothing to the pump**. It reaches the pump only through a transport that has no way to send a write.

## Running it

```
thermaestro probe 192.0.2.10
thermaestro probe 192.0.2.11 --protocol modbus-tcp --model S1255
```

The address is the gateway's (esphome-nibe, or another NibeGW), or an S-series pump's own. Options:

- `--protocol thermaestro-gw --key-file FILE`: use the Thermaestro gateway protocol, with the gateway's 64-digit key in `FILE` (not on the command line, where other users of the host could see it);
- `--protocol modbus-tcp --model S1255`: an S-series pump, which needs its model given: no documented register names it. Modbus TCP is turned on in the pump's menu 7.5.9, and the pump answers only addresses on the local network;
- `--read-port`, `--control-port`: if the gateway uses other ports than 9999 and 10090;
- `--modbus-port`: if the S-series pump answers on another port than 502;
- `--local-port`: for a gateway that sends to a fixed port;
- `--model F1245`: if a bus-family pump's own product name isn't recognized;
- `--minutes 5`: how long to capture;
- `--out DIR`: where to write the files.

It takes about the minutes given, and longer if some values haven't been read by then (up to two more minutes): on the bus, values the pump doesn't send by itself are read about one a second.

## What the files hold

- **`probe-<model>-<time>.json`**, the report:
  - the pump's family, product name, model, firmware and word order; for an S-series pump, also what it answers to Modbus's device identification, if anything;
  - which extra climate systems the model can have, and what detection found, pools too;
  - each of Thermaestro's points that the model's register map has, with its value and quality;
  - those it lacks, those the pump gave no value for or said it hasn't got, and which control levers Thermaestro could offer;
  - on the bus, which registers the pump sends by itself, and how many bus exchanges of each kind were seen.

  It holds no network address, key or personal name.
- **`probe-<model>-<time>-capture.jsonl`**, the capture, one exchange per line with its time: on the bus, every exchange the gateway forwarded, other devices' traffic too; over Modbus TCP, each answer with the request it answers.

Look through both files before sending them.
