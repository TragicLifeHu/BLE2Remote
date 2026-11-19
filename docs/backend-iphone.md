# BLE link backend: jailbroken iPhone

The first BLE link backend. A Jailbroken iPhone placed next to the heater holds the
BLE connection with CoreBluetooth and runs `backends/ios/blepipe.py`. By default it also runs the control
plane (`control_plane/server.py`) so the whole bridge is one device; the control plane can instead run
elsewhere (see [Split deployment](#split-deployment)).

Tested on: iPhone 7, iOS 15, Dopamine (roothide) jailbreak.

## What runs on the phone
`backends/ios/install.sh` installs two launchd daemons:

| Daemon | Program | Listens |
|---|---|---|
| `com.ble2remote.blepipe` | `blepipe.py` with a Bluetooth-entitled Python | `127.0.0.1:7700` (`--pipe-lan`: `0.0.0.0:7700`) |
| `com.ble2remote.app` | `app/server.py` (control plane) | `0.0.0.0:8080` |

Files live in `/var/mobile/ble2remote` (`data/` = pairing, schedule, logs; `lib/` = third-party Python packages).

## Requirements
- Python 3 from Sileo and `rubicon-objc` unpacked into `/var/mobile/ble2remote/lib`.
- OpenSSH, with the default `mobile` / `root` passwords changed.
- CoreBluetooth needs `com.apple.private.tcc.allow` (Bluetooth) → `install.sh` signs a Python copy into
  `/usr/local/bin/python3-ble` with `backends/ios/python-ble.entitlements` (launchd only spawns binaries inside
  the jbroot).

## Install / update
From the PC (password from `BLE2REMOTE_SSH_PASSWORD` or prompted):
```powershell
.\.venv\Scripts\python backends\ios\tools\deploy.py <iphone-ip>                 # upload and run install.sh
.\.venv\Scripts\python backends\ios\tools\deploy.py <iphone-ip> --no-install    # upload, restart the app daemon only
.\.venv\Scripts\python backends\ios\tools\deploy.py <iphone-ip> -- --pipe-lan   # also expose the raw pipe on :7700
```
Web app: `http://<iphone-ip>:8080`.

## Split deployment
To run the control plane on another machine (e.g. a Home Assistant host), install with `-- --pipe-lan`
and start the control plane there with `--pipe <iphone-ip>:7700`. Consider adding `--token` to
`blepipe.py` (and `--pipe-token` to the server), since the raw pipe has no other access control.
The phone's `com.ble2remote.app` daemon can then be unloaded.

## iPhone-specific behaviour
- Reconnects use the saved iOS peripheral id (`retrievePeripheralsWithIdentifiers` + a pending connect);
  scans are heavily throttled while the screen is locked.
- A locked iPhone suspends the CPU between radio events, which would stop the 20 s keep-alive and let the
  heater drop the link every ~60 s. blepipe holds an IOKit `PreventUserIdleSystemSleep` assertion
  (no entitlement needed) to prevent that.
- After a full reboot the jailbreak — and with it the bridge — must be re-activated manually.
  Keep the phone charging.
