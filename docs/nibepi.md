# Coming from NibePi

Thermaestro can take over from NibePi on the same pump and gateway. This is the short way.

## 1. Copy NibePi's settings

NibePi keeps its settings in `/etc/nibepi/config.json`; in Docker, wherever its volume maps `/etc/nibepi`. Copy it to the computer you use Thermaestro's web pages from. It holds the MQTT password and the price token in plain text, so delete the copy afterwards.

NibePi 1.0 kept its settings in another shape, which Thermaestro doesn't read: set Thermaestro up by hand instead.

## 2. Let Thermaestro hear the gateway

Thermaestro talks to the pump's gateway as NibePi does, and both can use it at once. NibePi sends its requests to the gateway's ports 10000 (reads) and 10001 (writes); the import sets Thermaestro up the same way.
- **esphome-nibe** answers where each request came from, so nothing changes on the gateway.
- **A gateway that sends only to fixed addresses**, such as openHAB's NibeGW: add Thermaestro's host as a target beside NibePi's, and the port it sends to as the local port under Setup → Pump.
- **The Thermaestro gateway protocol**, from [Thermaestro's fork of esphome-nibe](https://github.com/fnordpojk/esphome-nibe) or `thermaestro-gateway`, reports what happened to every request. It is set up under Setup → Pump, with the gateway's key.
- **A serial port:** NibePi read the pump over RS485 on its own host. Thermaestro always goes through a gateway: run `thermaestro-gateway` there, once NibePi has stopped, since only one program can hold the port.

## 3. The MQTT broker

If NibePi's broker ran on NibePi's own host (`127.0.0.1` or `localhost`), Thermaestro's host may not have one. The import checks: if none answers, install one, Mosquitto from your system's packages, or a broker container next to Thermaestro's. In Docker, `localhost` is the container itself: use the broker container's name or the host's address.

## 4. Read the file

Under **Setup → From NibePi**, choose `config.json`. Thermaestro shows what it would make, and nothing changes until you confirm:
- the pump connection;
- the MQTT broker and Home Assistant discovery, the password into the secrets file;
- the sensors, each room sensor one of NibePi's features used becoming a room of its climate system;
- the location, and the time zone from the price area;
- the spot price for NibePi's price area, or Tibber with its token;
- NibePi's VAT, markup, energy tax and transfer fee, offered unticked, since they may be out of date.

Every key in the file is listed as carried over, translated, or left out, and why. To see the same without changing anything, on the host: `thermaestro import nibepi config.json`.

NibePi's control settings (price levels, offsets, the hot-water features) aren't carried over yet: set what you want on the Intents page.

## 5. Run both for a while

Thermaestro can run beside NibePi in shadow: it decides and shows what it would change, and changes nothing. **Shadow vs the pump** sets each decision beside what the pump showed. Before you switch a setting to control, turn off NibePi's feature for it, or the two fight over it: Thermaestro lets go of a setting another program changes.

## 6. Stop NibePi, and review the pump's settings

NibePi changed some of the pump's own settings and left them so: the hot-water period (47134) held at 0 by its hot-water learning, which makes hot water wait behind heating; the heating offset at the last price level's; the hot-water mode; and others, depending on what was switched on. Once NibePi is stopped for good, **Setup → From NibePi → The pump's settings after NibePi**:

1. **NibePi is stopped:** while it runs, it keeps writing.
2. **Read the pump's settings:** each register NibePi is known to change, if the pump has it, with what NibePi did, its value now, its factory default, and its value before NibePi where NibePi kept it (the hot-water period). Putting the hot-water period back is recommended.
3. Keep what you want kept: that is the default. Otherwise set a register to its default, to its value before NibePi, to NibePi's own heating offset, or to a value you enter. Each change needs the password again, and is read back and recorded.
4. For a setting Thermaestro changes, the heating offset or the hot-water mode, you can choose what Thermaestro puts it back to instead. Once Thermaestro controls a setting, that is the only way: a direct change would only be put back.
5. **Compare every setting**, if you want: every setting the pump lets be changed, about 550 on an F1245, is read, about a second each, and those that differ from the factory default are listed with the same choices. Changes NibePi made that aren't on the list, such as values sent by hand, only show up here; an installer's or your own choice differs too.

NibePi's word order (48852) is shown and never changed: Thermaestro reads the pump correctly either way.

## 7. What's different

- What you want, not how: intents instead of price levels and offsets per level.
- Thermaestro never resets the pump's alarms. NibePi reset whatever alarm was active each time it started.
- The pump's operating priority is left alone.
- NibePi's Home Assistant entities go away and Thermaestro's come: automations using them need redoing.
- Commands over MQTT need rights, given by an administrator.
- No counterpart: Plejd, the RMU 40 emulation, CO₂ fan control, the compressor frequency lock, degree-minute resets, auto-luxury, the "Send value" form.
