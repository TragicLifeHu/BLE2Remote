# BLE2Remote — BLE Heater control plane

Turns a BLE-only baseboard heater (Example Device: AirMeter UAF028, was integrated into uHome Life platform) into a
scheduled, remotely controllable smart device.

The project is split into a **control plane** (plain Python, standard library only) and a thin
**BLE link** that relays raw frames between the heater and the control plane over a small TCP line
protocol. Anything that can hold a BLE connection to the heater and speak that protocol can be the link;
the control plane does not care where it runs.

```
                  BLE link (any backend)                     control plane (generic Python)
Heater ──BLE──>  blepipe-compatible pipe  ──TCP :7700──>  app server (:8080)  ──>  web app / REST API
                 · iPhone (implemented)                    · protocol + controller   ──>  MQTT / Home Assistant (TODO)
                 · simulator (development)                 · scheduler + safety checks
                 · ESP32 / Linux bleak (planned)           · activity log, pairing UI
```

See [docs/architecture.md](docs/architecture.md) for the layers and the pipe protocol a new link backend
has to implement.

## Features
- Control: power, 18–35 °C, power-save, off-timer / on-timer, voice control. Controls that the heater
  ignores in the current state (e.g. temperature while off) are disabled; commands ignored because of the
  child lock are detected and flagged.
- Schedule (control-plane local time): weekly rules, one-off events, skip-once, pause until a date,
  catch-up of missed events after a power cut, maximum on-time safety check.
  Manual / remote changes hold until the next event. "On" actions can optionally run "for N hours" using the
  heater's own off-timer; a 7-day conflict check blocks same-minute clashes and warns when a running timer
  would cut off a later event.
- Web app (installable PWA): home, schedule, log (7-day run history + activity log), pairing
  (scan → pick heater), Bluetooth release for using other control.
- REST API (`control_plane/server.py` docstring lists the endpoints).
- Home Assistant via MQTT discovery: draft in `control_plane/mqtt_bridge.py`, not finished — see the
  [roadmap](docs/roadmap.md#todo-home-assistant-integration).

## Layout
| Path | Layer | What |
|---|---|---|
```
control_plane/          generic control plane (Python stdlib only) — runs anywhere
  server.py               HTTP server, REST API, scheduler loop
  heater.py               pipe client, state cache, command verification, pairing, activity log
  controller.py           desired state -> safe command plan
  scheduler.py            weekly rules, events, catch-up, conflict / safety checks
  protocol.py             heater frame encode/decode
  store.py                JSON persistence
  mqtt_bridge.py          optional MQTT / Home Assistant discovery (draft)
  static/                 web app (PWA)
backends/               BLE link implementations (speak the pipe protocol)
  ios/                    jailbroken iPhone: blepipe.py (CoreBluetooth), install.sh, entitlements
    tools/                  deploy.py (upload + install), ssh_run.py, ssh_tunnel.py
  simulator/              fake_pipe.py — heater simulator for development without hardware
tools/                  PC dev tools: ble_scan/ble_gatt/ble_send (bleak), pipe_client, make_splash
tests/                  pytest: protocol, controller, scheduler, heater link
re/                     reverse-engineering material: Frida hook, captures, spike (see docs/reverse-engineering.md)
docs/                   architecture, heater protocol, iPhone backend, device recon, reverse engineering, roadmap
```
Docs: [architecture](docs/architecture.md) · [heater protocol](docs/protocol.md) ·
[iPhone backend](docs/backend-iphone.md) · [device recon](docs/device-recon.md) ·
[reverse engineering](docs/reverse-engineering.md) · [roadmap](docs/roadmap.md)

## Quick start (no hardware)
```powershell
python -m venv .venv; .\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python -m pytest tests
.\.venv\Scripts\python backends\simulator\fake_pipe.py --port 7799
.\.venv\Scripts\python control_plane\server.py --pipe 127.0.0.1:7799 --http 127.0.0.1:8099 --data $env:TEMP\heater-data
```
Open http://127.0.0.1:8099.

## Running the control plane
The control plane needs only Python 3 (standard library). Point it at any pipe:
```
python control_plane/server.py --pipe <pipe-host>:7700 [--pipe-token T] --http 0.0.0.0:8080 --data <dir>
```
It can run next to the link (as the iPhone backend does) or on another machine — a home server, a
Raspberry Pi, the Home Assistant host — with the link started in LAN mode. For remote access, put it
behind a VPN such as Tailscale rather than exposing it to the internet (there is no login).

## BLE link backends
| Backend | Status | Docs |
|---|---|---|
| Jailbroken iPhone (CoreBluetooth, `backends/ios/blepipe.py`) | working, in daily use | [docs/backend-iphone.md](docs/backend-iphone.md) |
| Simulator (`backends/simulator/fake_pipe.py`) | working, development only | — |
| ESP32 | planned | [roadmap](docs/roadmap.md) |
| Linux / Windows PC with bleak | planned | [roadmap](docs/roadmap.md) |

## License
MIT, see [LICENSE](LICENSE).
