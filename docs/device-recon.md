# Device recon — BLE baseboard heater

## Heater device (Example Scan)
| Field | Value |
|---|---|
| BLE address | `AA:BB:CC:DD:EE:FF` (placeholder; public-looking, also embedded in mfr data) |
| Local name | `UAF028` (GAP 0x2A00 = `554146303238`) |
| Adv service | `0000ffb0-…` |
| Manufacturer data | company `0x0AA5`: `<4-byte id>` `<6-byte MAC>` `0300` |
| RSSI @ PC | -50 dBm |
| MTU | 517 |

## GATT (read-only dump)
```
service 1800 Generic Access
  2a00 [read] = "UAF028"
service ffb0 (vendor)
  ffb1 h=0x0005 [read, write-without-response]   <- likely command TX
  ffb2 h=0x0007 [read, notify]  CCCD=0000         <- likely status RX
  ffb5 h=0x000a [indicate]      CCCD=0000         <- ? ack/handshake
  ffb4 h=0x000d [write]                           <- ? config/OTA
```
No unsolicited notifications in 15 s after subscribing to ffb2 + ffb5: device appears to be request/response.
