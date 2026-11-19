# Architecture

BLE2Remote has two layers joined by a plain-text TCP protocol:

```
┌────────┐   BLE    ┌────────────────┐   TCP lines, :7700   ┌────────────────────┐   HTTP :8080 / MQTT
│ Heater │◄────────►│ BLE link       │◄────────────────────►│ Control plane      │◄──────────────────► web app, REST,
└────────┘ FFB1/FFB2│ (any backend)  │                      │ control_plane/     │                     Home Assistant
                    └────────────────┘                      └────────────────────┘
```

- The **BLE link** is deliberately dumb: it owns the radio and relays raw frames. It has to sit within BLE
  range of the heater (a few metres).
- The **control plane** holds all the heater knowledge, state, schedule and UI. It is plain Python
  (standard library only) and can run anywhere that can reach the link over the network.

Keeping the link small means a new piece of hardware only has to implement the pipe protocol below;
everything users see is unchanged.

## Deployment options
| Setup | BLE link runs on | Control plane runs on | Notes |
|---|---|---|---|
| Single host | a PC / Raspberry Pi / server with Bluetooth near the heater | same host | simplest; needs a bleak-based link (planned) |
| Split | a small device near the heater (ESP32, old phone, Pi Zero) | home server, NAS or Home Assistant host | the link exposes `:7700` on the LAN; use a token |
| All-in-one device | the same device as the link | the same device | how the iPhone backend is installed by default |
| Development | `backends/simulator/fake_pipe.py` | your PC | no hardware needed |

Remote access should go through a VPN (e.g. Tailscale) or a reverse proxy with authentication — the web app
and REST API have no login, and the pipe only has an optional shared token.

## BLE link
Responsibilities — everything that depends on the radio, nothing that depends on heater semantics:
- **Pairing:** scan for devices advertising service `FFB0`, pair with the one the user picks, forget it;
  remember the pairing across restarts.
- **Connection:** connect, subscribe to notifications on `FFB2`, reconnect automatically after a drop or
  a heater power cut.
- **Keep-alive:** write the keep-alive frame (`aa0300ff000255`, configurable) after 20 s without other
  writes. The heater drops a link that is idle for ~60 s, so the link host must not sleep through
  that timer.
- **Relay:** write frames to `FFB1` (write-without-response) and forward notifications as `NOTIFY`.
- **Release:** disconnect on request for a given time so another client (the vendor's
  Program) can use the heater, then reconnect.

It never builds or interprets heater commands (apart from the keep-alive bytes) — see
[protocol.md](protocol.md) for those.

### Backends
| Backend | Location | Device id | Status |
|---|---|---|---|
| Simulator | `backends/simulator/fake_pipe.py` | fixed fake id | working — simulates the verified heater behaviour, incl. child lock |
| Jailbroken iPhone (CoreBluetooth) | `backends/ios/` | iOS peripheral UUID | working — [backend-iphone.md](backend-iphone.md) |
| PC / Raspberry Pi (bleak) | — | MAC address (Linux/Windows), UUID (macOS) | planned |
| ESP32 (NimBLE + Wi-Fi) | — | MAC address | planned |

## Control plane (`control_plane/`)
| Module | Role |
|---|---|
| `protocol.py` | heater frame encode/decode ([protocol.md](protocol.md)) |
| `heater.py` | pipe client (auto-reconnects to the link), state cache, command worker with verification (detects child-lock rejection), pairing, activity log |
| `controller.py` | turns a desired state into a safe command plan (setpoint commit delay, power-save ordering) |
| `scheduler.py` | weekly rules, one-off events, skip/pause, catch-up after outages, conflict and max-on-time checks |
| `store.py` | JSON persistence in `--data` |
| `server.py` | HTTP server: web app (`static/`), REST API, scheduler loop |
| `mqtt_bridge.py` | optional MQTT + Home Assistant discovery (draft — [roadmap](roadmap.md#todo-home-assistant-integration)) |

```
python control_plane/server.py --pipe <link-host>:7700 [--pipe-token T] --http 0.0.0.0:8080 --data <dir>
```

## Pipe protocol
UTF-8 lines terminated by `\n` over TCP (default port 7700). One link may serve several clients; every
client receives every message. "Required" marks what the control plane needs for basic control; the
others enable pairing, signal display and the release feature.

Client → link:

| Line | Required | Meaning |
|---|---|---|
| `AUTH <token>` | if token set | must be the first line when the link was started with a token |
| `WRITE <hex>` | yes | write a frame to `FFB1` |
| `SCAN [seconds]` | pairing | scan for heaters (service `FFB0`) |
| `PAIR <id>` / `FORGET` | pairing | pair with a scanned device / forget the pairing |
| `RELEASE <seconds>` | optional | disconnect and stay away for that long (`0` = reconnect now) |
| `QUERYSTATE` | optional | re-send `STATE` / `PAIRED` / `INFO` |
| `PING` | optional | liveness check, answered with `PONG` |

Link → client:

| Line | Required | Meaning |
|---|---|---|
| `HELLO blepipe <ver>` | optional | greeting on connect |
| `STATE <state>` | yes | `unpaired` \| `searching` \| `connecting` \| `connected` \| `released`; sent on connect and on every change |
| `PAIRED <id or ->` | yes | current pairing; sent on connect and on change |
| `NOTIFY <char> <hex>` | yes | notification frame (`<char>` = `FFB2` for status) |
| `OK` / `ERR <reason>` | yes | reply to each command |
| `INFO <id> <name>` | optional | the connected device (shown on the home page) |
| `RSSI <dBm>` | optional | signal strength, e.g. every 30 s while connected |
| `DEVICE <id> <rssi> <mfr-hex or -> <name>` / `SCANDONE` | pairing | scan results |
| `PONG` | optional | reply to `PING` |

`<id>` is whatever identifies the device on that backend (see the table above); the control plane treats
it as opaque and only passes it back in `PAIR`.

### Writing a new backend
1. Implement the required lines; test with `tools/pipe_client.py` (raw commands, decoded status) and then
   with the control plane's web app.
2. Implement pairing (`SCAN`/`DEVICE`/`SCANDONE`/`PAIR`/`FORGET`) so users don't have to configure ids.
3. Make sure the keep-alive keeps running when the host is idle, and that reconnects work after a
   heater power cut (the heater comes back with setpoint 0 — the control plane handles that).
4. Add it under `backends/<name>/` with a short README, and list it in the table above.
