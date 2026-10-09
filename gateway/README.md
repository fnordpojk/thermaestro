# Thermaestro gateway

A gateway between a Nibe heat pump's accessory bus and Thermaestro, for a Linux machine with an RS485 adapter, such as a Raspberry Pi next to the pump. It answers on the bus as a MODBUS40 accessory and passes the pump's traffic on over UDP. It can run alone, with Thermaestro on another machine.

It speaks two protocols:

- **Plain NibeGW**, as esphome-nibe does, on a read port and a write port.
- **The Thermaestro gateway protocol** on its control port. It reports what happened to every request, with timing from the bus, checks its input, keeps health statistics, and can require a pre-shared key. See [the protocol](../docs/gateway-protocol.md).

```
thermaestro-gateway /dev/ttyUSB0
```

`thermaestro-gateway --help` lists the options: the ports (read 9999, write 10000, control 10090 by default; 0 turns one off), the addresses allowed to send requests, fixed forwarding targets, and the file with the control port's key.

**Status:** there is no release yet. Version 0.0.0 on PyPI is a placeholder that keeps the package name. The project lives at https://github.com/fnordpojk/thermaestro.

License: AGPL-3.0-or-later. Thermaestro is not affiliated with NIBE Energy Systems.
