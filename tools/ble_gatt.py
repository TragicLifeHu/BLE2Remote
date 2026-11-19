"""Read-only GATT dump: connect, list services/characteristics/descriptors,
read readable values, and optionally listen to notifications for N seconds.

  python tools/ble_gatt.py AA:BB:CC:DD:EE:FF --listen 15
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt

from bleak import BleakClient


async def main(addr: str, listen: float) -> None:
    async with BleakClient(addr, timeout=20) as c:
        print(f"connected {addr} mtu={c.mtu_size}")
        notifiable = []
        for s in c.services:
            print(f"service {s.uuid}  ({s.description})")
            for ch in s.characteristics:
                props = ",".join(ch.properties)
                line = f"  char {ch.uuid} h=0x{ch.handle:04x} [{props}]"
                if "read" in ch.properties:
                    try:
                        line += f" = {(await c.read_gatt_char(ch)).hex()}"
                    except Exception as e:  # noqa: BLE001
                        line += f" (read err {e})"
                print(line)
                for d in ch.descriptors:
                    try:
                        v = (await c.read_gatt_descriptor(d.handle)).hex()
                    except Exception as e:  # noqa: BLE001
                        v = f"err {e}"
                    print(f"    desc {d.uuid} = {v}")
                if "notify" in ch.properties or "indicate" in ch.properties:
                    notifiable.append(ch)
        if listen > 0 and notifiable:
            def cb(ch, data: bytearray):
                ts = dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]
                print(f"{ts} NOTIFY {ch.uuid} {bytes(data).hex()}")
            for ch in notifiable:
                await c.start_notify(ch, cb)
            print(f"listening {listen}s on {len(notifiable)} characteristic(s)...")
            await asyncio.sleep(listen)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("address")
    ap.add_argument("--listen", type=float, default=0)
    a = ap.parse_args()
    asyncio.run(main(a.address, a.listen))
