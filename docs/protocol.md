# Heater BLE protocol (AirMeter UAF028)

**Classification: case A — static, no pairing, no auth, no encryption.** Sequence byte is not validated
(PC replays starting at any seq worked). Captured 2026-11-09 via Frida (`re/captures/ble-session1.log`, `ble-session2-voice.log`)
and confirmed by direct writes from the PC (`tools/ble_send.py`). Implementation: `control_plane/protocol.py`.

## Transport
- Device: name `UAF028`, address `AA:BB:CC:DD:EE:FF`, advertises service `FFB0`
  (Platform scans with filter `FFB0`).
- Write commands to **FFB1** (write-without-response).
- Subscribe **FFB2** (notify) for status. App also subscribes FFB5 (indicate) — nothing seen on it yet.

## Framing
```
AA | LEN | PAYLOAD (LEN bytes) | CHK | 55        CHK = (LEN + sum(PAYLOAD)) & 0xFF
```

## Commands (LEN = 3, payload = SEQ CMD VAL)
| CMD | VAL | Meaning |
|---|---|---|
| `00` | `00` | Query status (device answers with a status frame) |
| `01` | `00`/`01` | Power off / on |
| `06` | `12`..`23` | Setpoint 18–35 °C (raw °C) |
| `09` | `00`/`01` | Power-save off / on |
| `05` | `00`..`18` | Timer hours (0 = cancel, max 24). Heater on: off-timer. Heater off: on-timer (see below) |
| `0F` | `00`/`01` | Voice control (on-device Chinese voice commands) off / on |
| `FF` | `00` | Keep-alive, sent by the app every 20 s while idle; no reply. Keeps the BLE link open (see below) |
| `FE`, `F8` | `00` | Sent by the app after connect; no reply observed. Purpose unknown, not required. |

## Status (notify on FFB2, LEN = 8)
```
AA 08 | 00 53 PW TM MD LV SP VC | CHK 55
```
| Byte | Meaning |
|---|---|
| `00 53` | constant (`53` = 'S' status) |
| PW | power 0/1 |
| TM | timer hours remaining (0 = none) |
| MD | mode: 0 normal, 3 power-save |
| LV | 2 while power-save, else 0 (meaning TBD — maybe heat level) |
| SP | setpoint °C |
| VC | voice control enabled 0/1 |

- Status is **pushed automatically after every state change**, and on query. A command that does not change
  state produces no notification (e.g. eco-off when already off).
- Setting the setpoint **cancels power-save** (MD/LV → 0).
- A new setpoint is **acknowledged immediately** in the status frame, but the heater only **commits it ~3 s
  later** (the display flashes the new value first).
- **Race:** power-save sent within that 3 s window is *reported* as on (and the display shows the
  power-save icon), but the delayed setpoint commit still takes over — the heater ran at full 24 °C power
  (observation 2026-11-09). The app therefore waits 3.5 s after a setpoint before sending anything else.
- In power-save the heater's display shows only the power-save icon, no temperature.
- The physical buttons and IR remote need two presses: the first wakes the display and shows the value,
  the second changes it. BLE commands apply directly (subject to the 3 s setpoint commit).
- No room temperature is reported.
- **After a power cut** the heater comes back **off** with setpoint **0** (status `aa0800530000000000005b55`)
  until a setpoint is set, even after power-on, while the heater itself runs at its **boot default of 23 °C**
  (user-experience). The control plane shows 23 °C in that case.
- BLE link loss is reported by iOS ~30 s after power loss (CBError 6); a pending reconnect completes
  automatically within seconds of the heater powering up (verified 2026-11-09).
- **Changes made with the IR remote are pushed too** (power, setpoint, power-save, timer set/cancel) —
  verified 2026-11-09, `re/captures/ir-remote-test.log`. HA state stays in sync.
- **Child lock** (set from the IR remote only) is not visible over BLE: no notification, no command, and the
  status frame is identical to the unlocked state. Enabling it clears power-save.
- **While child-locked, the heater silently ignores ALL BLE commands**, including power off (verified
  2026-11-09, `re/captures/childlock-test.log`: setpoint, power off/on and power-save had no effect,
  no notification, and queries still answered). The bridge can only infer the lock: a state-changing
  command that gets no status change within ~2 s plus an unchanged query means "rejected (child lock?)".

- **Keep-alive:** the heater **terminates an idle link after ~60 s** (CBError 7,
  "device disconnected from us") when no frame arrives. The keep-alive only keeps the *connection* (and
  with it the status push) alive; heating doesn't depend on it. Sending `FE`/`F8` after
  connect made no difference.
  - Pitfall: a locked iPhone suspends the CPU between radio events, so a userland 20 s timer never fires.
    Result was a connect/drop cycle every ~60 s (475 overnight, link durations in multiples of the 20 s
    keep-alive tick). blepipe now holds an IOKit `PreventUserIdleSystemSleep` assertion (works without
    an entitlement); that also keeps the app server's scheduler on time.
- **Timer while off = on-timer (verified 2026-11-10):** with the heater off, `05 NN` is accepted and reported
  (`PW=0 TM=NN`; setpoint and power-save are not accepted while off, `re/captures/off-state-test.log`).
  It is a delayed start: armed 1 h at 10:16:07, the heater switched itself on at 11:16:2x (60 min)
  with its last setpoint (23 °C), no power-save, and TM back to 0, so **no off-timer follows**.
  - Turning the heater on by hand (BLE power on) while an on-timer is pending **cancels it** (TM → 0);
    turning it off again does not bring it back.
  - TM reads in whole hours; the 1 h on-timer read 1 for the full hour.

## Open questions
- Meaning of LV and the FE/F8 commands; anything on FFB5.
