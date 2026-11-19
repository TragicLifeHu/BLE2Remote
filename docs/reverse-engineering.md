# Reverse engineering (iPhone)

How the heater protocol in [protocol.md](protocol.md) was worked out, and how to repeat it (e.g. for a
different model of the same family). Material lives in `re/`.

The protocol turned out to be static — no pairing, auth or encryption — so the work was just: watch what
the vendor app writes, replay it from a PC, then probe edge cases.

## 1. Find the device
From any PC with Bluetooth:
```powershell
.\.venv\Scripts\python tools\ble_scan.py                    # nearby devices, name, RSSI, manufacturer data
.\.venv\Scripts\python tools\ble_gatt.py <mac> --listen 15  # read-only GATT dump + notifications
```
Results: [device-recon.md](device-recon.md).

## 2. Capture the vendor app's BLE traffic
The heater is controlled from a WeChat Mini Program, which talks to the heater through the host app's
CoreBluetooth calls. `re/frida/hook_corebluetooth.js` hooks those calls inside WeChat and logs scans,
connects, subscriptions, writes and notifications, one line per event.

This needs a jailbroken iPhone with **Frida** installed (repo `https://build.frida.re`, rootless build;
same major version as `frida-tools` on the PC) and WeChat with the heater's Mini Program.

```powershell
.\.venv\Scripts\frida-ps -U | Select-String WeChat     # phone on USB, trusted
$n = Get-Date -Format yyyyMMdd-HHmm
.\.venv\Scripts\frida -U -n WeChat -l re\frida\hook_corebluetooth.js -o re\captures\ble-$n.log
```
In the Frida REPL type `mark("label")` before each tap in the app (e.g. `mark("set 21")`), so the log can
be read action by action. Attach Frida *before* opening the Mini Program, otherwise the connect/subscribe
lines are missing.

Without USB, `re/tools/capture_wechat.sh` waits for WeChat to start and attaches over an SSH tunnel
(`backends/ios/tools/ssh_tunnel.py`).

Captures: `re/captures/ble-session1.log` (all basic controls), `ble-session2-voice.log` (voice control).

## 3. Replay from a PC
`tools/ble_send.py` writes frames directly with bleak — either raw captured bytes or `cmd:val` pairs that
it frames and checksums. Replays with any sequence number worked, which confirmed the protocol is
static.

## 4. Probe behaviour
With the bridge running, `tools/pipe_client.py` sends commands through the BLE link and decodes the
status frames, which makes edge-case tests quick:

| Capture | What it showed |
|---|---|
| `re/captures/ir-remote-test.log` | changes from the IR remote are pushed as status notifications |
| `re/captures/childlock-test.log` | with child lock on, every BLE command is silently ignored |
| `re/captures/off-state-test.log` | a timer set while off is an on-timer; setpoint / power-save are refused while off |

`re/tools/ble_spike.py` is the first feasibility test for the iPhone backend (can a plain process use
CoreBluetooth on a jailbroken phone); kept for reference.
