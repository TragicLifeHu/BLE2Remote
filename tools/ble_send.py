"""Send BLE heater frames from the PC and print notifications.

  python tools/ble_send.py raw aa030200000555 aa0303fe000455
  python tools/ble_send.py cmd 00:00 fe:00 f8:00        # cmd:val, framed + seq + checksum
  python tools/ble_send.py cmd 06:15 --listen 5         # set 21 C
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt

from bleak import BleakClient

ADDR = "AA:BB:CC:DD:EE:FF"  # placeholder: your heater's address (see tools/ble_scan.py)
TX = "0000ffb1-0000-1000-8000-00805f9b34fb"
RX = ["0000ffb2-0000-1000-8000-00805f9b34fb", "0000ffb5-0000-1000-8000-00805f9b34fb"]


def frame(seq: int, cmd: int, val: int) -> bytes:
    body = bytes([3, seq & 0xFF, cmd & 0xFF, val & 0xFF])
    return b"\xaa" + body + bytes([sum(body) & 0xFF]) + b"\x55"


async def run(frames: list[bytes], listen: float, gap: float, addr: str) -> None:
    async with BleakClient(addr, timeout=20) as c:
        def cb(ch, data: bytearray):
            ts = dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]
            print(f"{ts} NOTIFY {ch.uuid[4:8]} {bytes(data).hex()}", flush=True)
        for u in RX:
            await c.start_notify(u, cb)
        for f in frames:
            ts = dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]
            print(f"{ts} WRITE  ffb1 {f.hex()}", flush=True)
            await c.write_gatt_char(TX, f, response=False)
            await asyncio.sleep(gap)
        await asyncio.sleep(listen)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["raw", "cmd"])
    ap.add_argument("items", nargs="+")
    ap.add_argument("--seq", type=int, default=2)
    ap.add_argument("--gap", type=float, default=1.0)
    ap.add_argument("--listen", type=float, default=5.0)
    ap.add_argument("--addr", default=ADDR)
    a = ap.parse_args()
    if a.mode == "raw":
        frames = [bytes.fromhex(x) for x in a.items]
    else:
        frames = []
        for i, it in enumerate(a.items):
            c, v = it.split(":")
            frames.append(frame(a.seq + i, int(c, 16), int(v, 16)))
    asyncio.run(run(frames, a.listen, a.gap, a.addr))


if __name__ == "__main__":
    main()
