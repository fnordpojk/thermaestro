# The Thermaestro gateway protocol (`thermaestro-gw`), version 0

`thermaestro-gw` is the protocol's id. `thermaestro-gateway` is a program that implements it: the Python gateway in this repository (§14).

**Status: draft.** The major version is 0 until the protocol is stable; version 1 will be the first stable one, and an incompatible change on purpose. Features are added as options (§5), so most growth needs no version change.

## 1. What it is for

A gateway sits on a Nibe heat pump's accessory bus (RS485, 9600 baud) and answers there as the MODBUS40 accessory. Clients talk to it over UDP. The established way to do that is the NibeGW protocol (openHAB's NibeGW, esphome-nibe): a client sends a request frame to a UDP port, the gateway queues it and hands it to the pump at the next matching token, and every completed bus exchange is forwarded to the clients.

Plain NibeGW says nothing about a request after it is sent:

- whether it was queued, dropped from a full queue, sent, or ACKed by the pump;
- which of the pump's `0x6A` read answers or `0x6C` write results answers which request;
- when any of it happened;
- how healthy the bus and the gateway are.

This protocol adds, on its own UDP port beside the plain NibeGW ports, which stay exactly as they are:

1. **request ids and fate reports**, and answers tagged with the request's id;
2. **gateway timestamps** on every event;
3. **validation before the bus**, with capped queues whose refusals are reported;
4. **subscriptions and health**, for several clients at once;
5. **a version handshake**, so a client can tell a gateway that speaks it from a plain NibeGW gateway and fall back;
6. **a bus path that never waits on the network**: everything the gateway sends is non-blocking, and what it can't send is counted;
7. **health counters that expose stalls**, not just averages: the longest gap between passes of the gateway's loop, and the pump's NAKs of the gateway's replies;
8. **optional authentication** with a pre-shared key (§11).

The gateway doesn't parse payloads, with one exception: it reads the register number of a `0x6A`, to pair answers (§8).

## 2. Background: the bus

The pump is the bus master. Its telegrams are `5C ADDR_HI ADDR_LO CMD LEN DATA… CHK`; an accessory's reply is `C0 CMD LEN DATA… CHK`.

- LEN counts the data bytes as sent. The checksum is the XOR of everything after the start byte (for a reply, including the `C0`), and is sent as `0xC5` when it comes out as `0x5C`. A `0x5C` inside a telegram's data is sent doubled and stands for one.
- MODBUS40 is address `0x0020`. The pump sends it tokens (telegrams without data) that invite a reply: `0x69` read, `0x6B` write, `0xEE` accessory. A reply to a token, or an ACK (`0x06`) if there is nothing to send, must come at once; after a reply, the pump sends ACK, or NAK (`0x15`) on a bad checksum.
- A read request is the reply `C0 69 02 REG_LO REG_HI CHK`. The value comes later in a `0x6A` telegram whose data is the register number (little-endian) and two 16-bit words: that register's and the next one's. On the pumps this was developed against, it came about 1 s after the pump took the request.
- A write request is `C0 6B 06 REG_LO REG_HI VALUE(4 bytes, LE) CHK`. The pump reports the result in a `0x6C` telegram with one byte: 1 accepted, 0 refused. **Accepted isn't applied**: a pump can accept a write and keep its old value, so only a read afterwards shows the result.
- The pump also sends `0x68` (its LOG.SET data stream) and `0x6D` (product information) to MODBUS40, and telegrams to other accessories' addresses, which those devices answer.

A plain NibeGW gateway takes requests on one UDP port per (address, token), by convention 9999 for MODBUS40 reads and 10000 for writes. It keeps a queue of at most 3 per port and drops the oldest when it is full. It forwards every completed exchange (the telegram, the reply that followed, and the closing ACK/NAK) as one datagram to every client heard from in the last 120 s.

## 3. Transport and framing

- **A separate UDP port, the control port**, by default **10090**. A client speaks this protocol only there. Some plain gateways put any datagram on their ports onto the bus unvalidated, so a handshake sent to a plain port could become a bogus reply on the bus; a port that plain gateways don't use has no such risk.
- **Binary, little-endian, one message per datagram**, at most **512 bytes**.

**Header (20 bytes):**

| offset | size | field | meaning |
|---|---|---|---|
| 0 | 2 | `magic` | `0x54 0x47` ("TG") |
| 2 | 1 | `ver_major` | incompatible changes; 0 for this version |
| 3 | 1 | `ver_minor` | informational: the registry revision the sender was built against (§7); 1 here |
| 4 | 1 | `type` | message type (§6) |
| 5 | 1 | `flags` | bit 7: authenticated (§11); the others are 0 |
| 6 | 2 | `body_len` | bytes after the header, not counting the authentication trailer |
| 8 | 4 | `id` | a request id, or an event sequence number (§7.8) |
| 12 | 8 | `gw_time_us` | the gateway's monotonic clock in µs since boot; 0 in client messages |

A receiver checks, in this order: at least 20 bytes (`malformed`), the magic (`bad_magic`), `ver_major` (`bad_version`), the type (`unknown_type`), that the datagram is exactly header + `body_len` + the trailer if bit 7 is set (`malformed`), the fixed core (`malformed`), then the options (§5).

## 4. The body

A message body is a **fixed core**, the few fields that message type always has (§6), followed by an **option list** filling the rest of the body. A core never changes within a major version.

## 5. Options

**One option:**

| size | field | meaning |
|---|---|---|
| 2 | `tag` | the option's number; **bit 15 = critical** |
| 2 | `len` | value length in bytes |
| `len` | `value` | integers little-endian at the width the registry states; strings UTF-8 without a terminator |

**Rules:**

- **Unknown and not critical:** ignored. This is how new features stay safe with older receivers.
- **Unknown and critical:** the receiver refuses the whole message with ERROR `unsupported_option` naming the tag; for a REQUEST it also sends FATE `DROPPED(UNSUPPORTED_OPTION)`. A sender can insist this way: "if you don't understand this, don't act on my message."
- **Order is free.** An option appears **once** unless the registry marks it *repeatable*. A second copy of a non-repeatable option, a length running past the body, a known option with the wrong length, text that isn't UTF-8 or is longer than its limit: `malformed`, and the message is refused.
- **Request and grant share a tag.** In HELLO an option says what the client asks for; the same tag in WELCOME says what the gateway granted. An option asked for but missing from WELCOME is not granted.
- **The authentication trailer is not an option** (§11).

**Tag ranges** (the critical bit comes on top):

| range | used in |
|---|---|
| `0x0001`–`0x00FF` | session options: HELLO, SUBSCRIBE, echoed in WELCOME |
| `0x0100`–`0x01FF` | gateway information: WELCOME |
| `0x0200`–`0x02FF` | ERROR details |
| `0x0300`–`0x03FF` | HEALTH values |
| `0x0400`–`0x04FF` | per-request and per-event options: REQUEST, FATE, ANSWER, FRAME |
| `0x0800`–`0x0FFF` | features with parameters, asked for in HELLO and granted in WELCOME |
| `0x7F00`–`0x7FFF` | implementation-private diagnostics; never critical |

**Versions:** `ver_major` changes only if the header or a fixed core changes; a client and a gateway with different majors don't talk (§13). `ver_minor` is informational. Adding a feature means a new tag, asked for in HELLO and granted (or not) in WELCOME.

## 6. Messages

**Client → gateway**

| type | name | fixed core | options |
|---|---|---|---|
| `0x01` | HELLO | `ver_min` u8, `ver_max` u8: the range of major versions the client speaks | `CLIENT_NAME`, `CLIENT_NONCE` (§11), `SUBSCRIBE`, `LEASE_S`, `HEALTH_INTERVAL_S`, feature options |
| `0x02` | KEEPALIVE | none | none. Renews the lease |
| `0x03` | BYE | none | none. Ends the session |
| `0x04` | SUBSCRIBE | none | `SUBSCRIBE`: the new mask |
| `0x10` | REQUEST | `address` u16, `token` u8, `flags` u8 (bit 0 PRIORITY, bit 1 EXPECT_ANSWER), `ttl_ms` u16 (0: no limit), `answer_timeout_ms` u16 (0: the gateway's default), `frame_len` u8, `frame`: the `C0` reply frame to send on the token | none yet |
| `0x11` | CANCEL | none; the header `id` names the queued request | none |

**Gateway → client**

| type | name | fixed core | options |
|---|---|---|---|
| `0x81` | WELCOME | `ver_major` u8, `ver_minor` u8: the gateway's | `BOOT_ID`, `FEATURES`, `GATEWAY_NONCE` (with a key), `IMPL`, `IMPL_VERSION`, `UPTIME_S`, `QUEUE_CAP`, `MAX_CLIENTS`, `ANSWER_TIMEOUT_MS`, `TIMESTAMP_LAG_MAX_US`, `PLAIN_PORTS`, `ACK_ADDRESS` (repeated), and the granted `SUBSCRIBE`, `LEASE_S`, `HEALTH_INTERVAL_S` |
| `0x90` | FATE | `stage` u8, reserved u8 (0), `detail` u16, `stage_time_us` u64 | none yet |
| `0x91` | ANSWER | `status` u8, `frame_len` u8, `frame`: the pump's `0x6A`/`0x6C` telegram exactly as received (empty on TIMEOUT) | none yet |
| `0xA0` | FRAME | `kind` u8, `origin` u8, `request_id` u32 (0 unless this client's request), `t_complete_us` u64, `t_reply_us` u64 (0 if the gateway sent no reply), `data_len` u16, `data`: the exchange's bytes exactly as a plain NibeGW datagram carries them | none yet |
| `0xB0` | HEALTH | none | the values of §10, one option each |
| `0xE0` | ERROR | `code` u16 | `ERR_TAG` (the offending option), `ERR_DETAIL`. The header `id` echoes the offending message's |

## 7. Registry, revision 1

### 7.1 FATE `stage` (u8)

1 `QUEUED`, 2 `SENT`, 3 `PUMP_ACK`, 4 `PUMP_NAK`, 5 `NO_ACK_SEEN`, 6 `DROPPED`.

`detail` for `QUEUED` is the number of requests ahead of this one in its queue (0: next on the token); for `DROPPED`, the reason below; otherwise 0.

### 7.2 Drop reasons (u16)

1 `INVALID_FRAME` (not a valid `C0` frame), 2 `TOKEN_MISMATCH` (the frame's command isn't the token), 3 `UNKNOWN_KEY` (no queue for that address and token), 4 `QUEUE_FULL`, 5 `EXPIRED` (the ttl passed before the token came), 6 `CANCELLED`, 7 `EVICTED` (a plain request pushed it out), 8 `UNSUPPORTED_OPTION`, 9 `SHUTDOWN`.

### 7.3 ANSWER `status` (u8)

1 `OK`, 2 `TIMEOUT`, 3 `AMBIGUOUS`.

### 7.4 FRAME `kind` (u8)

1 `TO_GATEWAY`: a telegram to an address this gateway answers for, with what the gateway sent back and the pump's byte after a reply. 2 `TO_OTHER`: a telegram to another address, with that device's reply and the closing byte, if any. 3 `UNPARSED`: bytes the gateway completed as an exchange but couldn't parse (a bad checksum, an overlong frame).

### 7.5 FRAME `origin` (u8)

Who supplied the gateway's reply, set per recipient: 0 `NONE` (no reply from the gateway), 1 `ACK_ONLY` (the gateway ACKed or NAKed, with no queued request), 2 `PLAIN_CLIENT`, 3 `THIS_CLIENT` (`request_id` set), 4 `OTHER_CLIENT` (`request_id` 0, since ids are per client), 5 `CONSTANT` (a configured fixed reply).

### 7.6 ERROR `code` (u16)

1 `bad_magic`, 2 `bad_version`, 3 `unknown_type`, 4 `malformed`, 5 `unsupported_option`, 6 `not_subscribed`, 7 `too_many_clients`, 8 `source_refused`, 9 `auth_required`, 10 `auth_failed`, 11 `replay`, 12 `no_session` (a message other than HELLO from a sender without a session), 13 `unknown_request` (CANCEL of an id that isn't queued).

### 7.7 Options

Types are little-endian; "str" is UTF-8; R = repeatable.

| tag | name | type | in | meaning |
|---|---|---|---|---|
| `0x0001` | `CLIENT_NAME` | str ≤ 32 | HELLO | for logs and the gateway's client list |
| `0x0002` | `CLIENT_NONCE` | 16 bytes | HELLO | §11 |
| `0x0003` | `SUBSCRIBE` | u32 | HELLO, SUBSCRIBE; WELCOME | bit 0 `FRAMES_ALL`, bit 1 `FRAMES_OWN`, bit 2 `HEALTH` |
| `0x0004` | `LEASE_S` | u16 | HELLO; WELCOME | session lease |
| `0x0005` | `HEALTH_INTERVAL_S` | u16 | HELLO; WELCOME | HEALTH period |
| `0x0100` | `BOOT_ID` | u32 | WELCOME, HEALTH | random per gateway boot |
| `0x0101` | `GATEWAY_NONCE` | 16 bytes | WELCOME | §11 |
| `0x0102` | `IMPL` | str | WELCOME | e.g. `esphome-nibe`, `thermaestro-gateway` |
| `0x0103` | `IMPL_VERSION` | str | WELCOME | |
| `0x0104` | `UPTIME_S` | u32 | WELCOME, HEALTH | |
| `0x0105` | `FEATURES` | u32 | WELCOME | bit 0 fate, 1 answer pairing, 2 frames, 3 health, 4 priority, 5 ttl, 6 auth (a key is configured and required) |
| `0x0106` | `QUEUE_CAP` | u8 | WELCOME | per (address, token) |
| `0x0107` | `MAX_CLIENTS` | u8 | WELCOME | |
| `0x0108` | `ANSWER_TIMEOUT_MS` | u16 | WELCOME | the default |
| `0x0109` | `TIMESTAMP_LAG_MAX_US` | u32 | WELCOME | §9 |
| `0x010A` | `PLAIN_PORTS` | u16 read, u16 write | WELCOME | 0 where a plain port is off |
| `0x010B` | `ACK_ADDRESS` | u16, R | WELCOME | the addresses the gateway answers for |
| `0x0200` | `ERR_TAG` | u16 | ERROR | the option that caused it |
| `0x0201` | `ERR_DETAIL` | str | ERROR | human-readable |
| `0x0301`–`0x0319` | HEALTH values | §10 | HEALTH | |
| `0x0801`, `0x0802` | | | | reserved for future features |

**Subscription bits** are those of `SUBSCRIBE`. **REQUEST flags:** bit 0 `PRIORITY` (to the front of its queue), bit 1 `EXPECT_ANSWER` (pair the pump's answer with it).

### 7.8 Header `id` in gateway messages

FATE and ANSWER carry the request's id. FRAME and HEALTH carry a sequence number per session, shared by the two and starting at 1, so a client can count events it missed. WELCOME and ERROR echo the id of the message they answer (0 if it couldn't be read).

### 7.9 Defaults

Lease 120 s (granted between 10 and 600), health interval 10 s (1 to 3600), answer timeout 5 s, 4 clients, a queue of 3 per (address, token), datagrams of at most 512 bytes.

## 8. A request's life, and pairing answers

```
REQUEST ─► validate ─► DROPPED(INVALID_FRAME | TOKEN_MISMATCH | UNKNOWN_KEY | QUEUE_FULL)
             │
             ▼
          QUEUED ──(ttl passed when its token comes)──► DROPPED(EXPIRED)
             │   ──(CANCEL)───────────────────────────► DROPPED(CANCELLED)
             │   ──(a plain request pushes it out)────► DROPPED(EVICTED)
             ▼
           SENT (on the pump's token)
             │
             ├─► PUMP_ACK / PUMP_NAK / NO_ACK_SEEN   (the pump's byte after the reply)
             ▼
          ANSWER(OK | TIMEOUT | AMBIGUOUS)            (with EXPECT_ANSWER)
```

**Queues.**

- One queue per (address, token), **shared with plain requests**.
- **Plain requests behave as in plain NibeGW**: a full queue drops its oldest entry. If that entry was a protocol request, its owner gets `DROPPED(EVICTED)`.
- **A protocol request doesn't evict.** On a full queue it is refused with `QUEUE_FULL`, and the client decides whether to try again.
- `PRIORITY` puts a request at the front of its queue.
- **The ttl is checked when a token arrives** (and periodically): an expired request is dropped and the next one is tried, so a write that arrives after its client gave up never reaches the pump.
- When the gateway stops, every queued protocol request gets `DROPPED(SHUTDOWN)`.

**The pump's byte after the reply.** 0x06 or 0x15 gives `PUMP_ACK` or `PUMP_NAK`; if the next telegram starts instead, `NO_ACK_SEEN`. `PUMP_ACK` means the pump received the request frame. It is not a write result, which comes in `0x6C`, and certainly not "applied". A NAKed request was not taken, and gets no answer.

**Reads (`0x6A`): first in, first out per register.**

- The gateway keeps, for each register, the read requests the pump took (ACKed, or with no byte seen after them), in the order it took them, **from every client, plain ones included**.
- A `0x6A` answers the oldest of them. If that is a protocol request with `EXPECT_ANSWER`, its owner gets `ANSWER(OK)` with the telegram; a plain request's answer goes nowhere but the forwarded frame. Later requests wait for their own answers.
- **Why not give an answer to every waiting read of the register:** a read-back after a write would then get the answer to a request taken *before* the write, by this client or another, and show the old value.
- A request with no answer within its answer timeout gets `ANSWER(TIMEOUT)` and leaves the list, including when the next answer for that register arrives first, so a lost answer doesn't shift later pairings.
- This relies on the pump answering each read request it takes with one `0x6A`, in the order taken.

**Writes (`0x6C`): by order, one in flight.**

- A `0x6C` carries no register.
- So a protocol write isn't sent while another write the gateway sent (plain or protocol) awaits its `0x6C` or its timeout. On that write token the gateway sends the next eligible request, or just ACKs.
- A plain client's write can still be in flight at the same time. Every write the gateway sends is recorded in send order. If a `0x6C` arrives while more than one is in flight, it pairs with the oldest, and a protocol request gets `ANSWER(AMBIGUOUS)` rather than a guess.

**A gateway restart loses all state.** `BOOT_ID` changes; a client seeing a new `BOOT_ID` knows the fates of its earlier requests are unknown, and should check by reading.

## 9. Time

- `gw_time_us` is the gateway's monotonic clock since boot (64-bit µs), together with `BOOT_ID`.
- A frame's time is when the gateway **processed** its last byte, not when it crossed the wire. The gateway states the largest lag between the two in WELCOME as `TIMESTAMP_LAG_MAX_US`.
- **Mapping to the client's clock:** every message from the gateway is a sample. Its arrival time minus its `gw_time_us` is the clocks' offset plus the delay on the way, so the smallest recent sample is the best estimate. Forgetting old samples follows a drifting crystal. The gateway needs no NTP.

## 10. Health

HEALTH goes to sessions subscribed to it every health interval, and at once when the bus state changes. The bus counts as active while a byte has arrived within the last 5 s. Counters are u32 since boot unless marked "interval" (reset with each report). A gateway leaves out what it can't measure.

| tag | name | type |
|---|---|---|
| `0x0100` | `BOOT_ID` | u32 |
| `0x0104` | `UPTIME_S` | u32 |
| `0x0301` | `BUS_STATE` | u8: 0 silent, 1 active |
| `0x0302` | `MS_SINCE_LAST_BYTE` | u32 |
| `0x0303` | `MS_SINCE_LAST_TOKEN` | u32 (to an address the gateway answers for) |
| `0x0304` | `FRAMES_OK` | u32 |
| `0x0305` | `CRC_ERRORS` | u32 |
| `0x0306` | `NAKS_SENT` | u32 |
| `0x0307` | `INVALID_BYTES` | u32 |
| `0x0308` | `PUMP_NAKS` | u32: the pump NAKed the gateway's reply |
| `0x0309` | `NO_ACK_SEEN` | u32 |
| `0x030A` | `TOKENS_WITH_REPLY` | u32 |
| `0x030B` | `TOKENS_ACK_ONLY` | u32 |
| `0x030C` | `DROPS` | R: u16 reason, u32 count (6 bytes); only reasons that occurred |
| `0x030D` | `EVICTIONS` | u32 |
| `0x030E` | `AMBIGUOUS_ANSWERS` | u32 |
| `0x030F` | `ANSWER_TIMEOUTS` | u32 |
| `0x0310` | `LOOP_GAP_MAX_MS` | u32, interval: the longest gap between passes of the gateway's loop, the best available sign of a late reply |
| `0x0311` | `REPLY_PREP_MAX_US` | u32, interval: from processing a token to having the reply ready |
| `0x0312` | `UDP_SEND_ERRORS` | u32 |
| `0x0313` | `EVENTS_DROPPED` | u32 |
| `0x0314` | `CLIENTS` | u8 |
| `0x0315` | `QUEUE_DEPTH` | R: u16 address, u8 token, u8 depth (4 bytes) |
| `0x0316` | `AUTH_FAILURES` | u32, with a key |
| `0x0317` | `REPLAYS_REJECTED` | u32, with a key |
| `0x0318` | `WIFI_RSSI` | i8 dBm, where there is Wi-Fi |
| `0x0319` | `FREE_HEAP` | u32 bytes, where it is known |

A stall can't be reported while it happens; the next HEALTH reports it in `LOOP_GAP_MAX_MS`.

## 11. Authentication: an optional pre-shared key

Without a key, the control port works unauthenticated, like plain NibeGW. With one, **every message on the control port must be authenticated**.

**The key** is 32 random bytes, configured on the gateway and in the client, and never sent.

**The session key:**

- HELLO carries a 16-byte random `CLIENT_NONCE`; WELCOME carries a 16-byte random `GATEWAY_NONCE`.
- Both sides derive `session_key = HMAC-SHA256(key, "thermaestro-gw session" ‖ client_nonce ‖ gateway_nonce ‖ boot_id)`, the boot id as u32 little-endian.

**Every authenticated message ends with a trailer** after the body (header flag bit 7 set; `body_len` excludes it):

| size | field |
|---|---|
| 4 | `seq`: per session and per direction, starting at 1, strictly increasing |
| 16 | `mac`: the first 16 bytes of HMAC-SHA256(signing key, header ‖ body ‖ seq) |

- HELLO is signed with the **key itself**. Every later message in both directions, WELCOME included (its `seq` is 1), is signed with the **session key**, so a correct WELCOME also proves the gateway knows the key.
- A message whose `seq` isn't greater than the last one accepted in that direction gets ERROR `replay` and is counted. A replayed HELLO only starts a session the sender can't use without the key.
- A missing or wrong MAC gets ERROR `auth_failed` (signed, inside a session) and is counted; it never reaches a queue.
- **A sender without a session hears only `auth_required` or `auth_failed`**, unsigned and at most one ERROR a second.
- With a key configured, a HELLO without `CLIENT_NONCE`, or unsigned, gets `auth_required`.
- **A client can insist on authentication** by sending `CLIENT_NONCE` with the critical bit set. A gateway without a key then refuses it (`unsupported_option`) rather than open an unauthenticated session, so the client knows the gateway is unprotected.

**What a key doesn't cover:** the plain NibeGW ports stay unauthenticated. To make every write to the pump need the key, turn the plain write port off on the gateway.

## 12. Sessions and subscriptions

- A **session** starts with HELLO and lasts for the granted lease. Any authenticated message from the client renews it; KEEPALIVE exists for that. BYE ends it, and so does an expired lease, which drops the session's queued requests silently.
- HELLO from an address that has a session replaces it. A HELLO beyond the client limit gets `too_many_clients`; one whose `[ver_min, ver_max]` doesn't include the gateway's major gets `bad_version`. A gateway may restrict which addresses may use the control port (`source_refused`).
- A message other than HELLO from a sender without a session gets `no_session`, or `auth_required` with a key. ERRORs to one sender come at most once a second.
- **Subscriptions:** `FRAMES_ALL` (every exchange, with its metadata), `FRAMES_OWN` (only exchanges that carry this client's requests), `HEALTH`. FATE and ANSWER always go to a request's owner, whatever its subscription.
- A protocol client is not also a plain NibeGW target, so it doesn't get every frame twice.
- **Other writers become visible:** a FRAME's `origin` says whether a reply came from a plain client, another protocol client or a configured constant.

## 13. The client's side: handshake and fallback

1. Send HELLO to the control port, and twice more, a second apart, if nothing comes back.
2. **WELCOME:** use the protocol.
3. **No WELCOME, or another major version:** fall back to plain NibeGW on the read and write ports, with what plain NibeGW can promise. **Not when the client is configured with a key:** then it stops and reports that the gateway doesn't speak the protocol, since the plain ports are unauthenticated.
4. **ERROR `unsupported_option`:** the gateway doesn't know a critical option. The client retries without it if it can live without the feature, or stops and reports what is missing. It doesn't fall back to plain NibeGW when the missing option was the authentication it insisted on.
5. **WELCOME without an option the client asked for:** that feature isn't granted, and the client works without it.
6. **A new `BOOT_ID`**, `no_session`, or no HEALTH for a lease: send HELLO again. The fates of requests made before are unknown.

## 14. Implementations and test vectors

- **Python gateway:** the `thermaestro-gateway` package (`gateway/` in this repository), for an RS485 adapter on a Linux machine.
- **esphome-nibe:** an optional `thermaestro` block in a fork of [esphome-nibe](https://github.com/elupus/esphome-nibe), at [fnordpojk/esphome-nibe](https://github.com/fnordpojk/esphome-nibe), branch `thermaestro-protocol`.
- **Client:** `thermaestro.nibe.transport` in this repository.
- **Test vectors:** `testvectors/` holds byte sequences for every message type, invalid datagrams with the error each must give, and the authentication steps. Both implementations are tested against them. They are licensed AGPL-3.0-or-later or MIT, at your choice, so implementations under either license can use them. `scripts/make-testvectors.py` builds them with `struct` and `hmac` directly, not with the codec.
