"""BLE advertisement scanner for finding the heater.

Usage:
  python tools/ble_scan.py                 # scan 10 s, table sorted by RSSI
  python tools/ble_scan.py -t 20 --save on.json
  python tools/ble_scan.py --diff on.json off.json   # what vanished / appeared

Tip: scan with the heater ON, then unplug it and scan again, then --diff.
Close the other controls first: many BLE devices stop advertising while connected.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from bleak import BleakScanner


def _hex(b: bytes) -> str:
    return b.hex()


async def scan(seconds: float) -> dict[str, dict]:
    found: dict[str, dict] = {}

    def on_adv(device, adv):
        e = found.setdefault(device.address, {"address": device.address, "rssi": -999})
        e["name"] = adv.local_name or device.name or e.get("name")
        e["rssi"] = max(e["rssi"], adv.rssi)
        e["service_uuids"] = sorted(set(e.get("service_uuids", [])) | set(adv.service_uuids))
        md = e.setdefault("manufacturer_data", {})
        for cid, data in adv.manufacturer_data.items():
            md[f"0x{cid:04x}"] = _hex(data)
        sd = e.setdefault("service_data", {})
        for uuid, data in adv.service_data.items():
            sd[uuid] = _hex(data)
        if adv.tx_power is not None:
            e["tx_power"] = adv.tx_power

    async with BleakScanner(detection_callback=on_adv):
        await asyncio.sleep(seconds)
    return found


def print_table(found: dict[str, dict]) -> None:
    rows = sorted(found.values(), key=lambda e: e["rssi"], reverse=True)
    for e in rows:
        print(f"{e['rssi']:>5} dBm  {e['address']}  {e.get('name') or '-'}")
        if e.get("service_uuids"):
            print(f"            services: {', '.join(e['service_uuids'])}")
        for cid, data in e.get("manufacturer_data", {}).items():
            print(f"            mfr {cid}: {data}")
        for uuid, data in e.get("service_data", {}).items():
            print(f"            svcdata {uuid}: {data}")
    print(f"\n{len(rows)} devices")


def diff(a_path: str, b_path: str) -> None:
    a = json.load(open(a_path, encoding="utf-8"))
    b = json.load(open(b_path, encoding="utf-8"))
    gone = {k: v for k, v in a.items() if k not in b}
    new = {k: v for k, v in b.items() if k not in a}
    print(f"Only in {a_path} ({len(gone)}):")
    print_table(gone) if gone else print("  none")
    print(f"\nOnly in {b_path} ({len(new)}):")
    print_table(new) if new else print("  none")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-t", "--time", type=float, default=10.0)
    ap.add_argument("--save")
    ap.add_argument("--diff", nargs=2, metavar=("A", "B"))
    args = ap.parse_args()
    if args.diff:
        diff(*args.diff)
        return 0
    found = asyncio.run(scan(args.time))
    print_table(found)
    if args.save:
        with open(args.save, "w", encoding="utf-8") as f:
            json.dump(found, f, indent=2, ensure_ascii=False)
        print(f"saved {args.save}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
