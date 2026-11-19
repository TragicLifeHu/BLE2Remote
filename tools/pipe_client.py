"""Talk to blepipe over TCP and decode status frames.

  python tools/pipe_client.py <iphone-ip> query
  python tools/pipe_client.py <iphone-ip> setpoint=24 query --listen 5
  python tools/pipe_client.py <iphone-ip> raw=aa030200000555 release=30
Commands: query | power=on/off | setpoint=N | eco=on/off | timer=N | voice=on/off | raw=HEX | release=SEC
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "control_plane"))
import protocol as p  # noqa: E402

ON = {"on": True, "off": False, "1": True, "0": False}


def to_line(item: str) -> str:
    k, _, v = item.partition("=")
    if k == "query":
        return "WRITE " + p.query().hex()
    if k == "power":
        return "WRITE " + p.power(ON[v]).hex()
    if k == "eco":
        return "WRITE " + p.eco(ON[v]).hex()
    if k == "voice":
        return "WRITE " + p.voice(ON[v]).hex()
    if k == "setpoint":
        return "WRITE " + p.setpoint(int(v)).hex()
    if k == "timer":
        return "WRITE " + p.timer(int(v)).hex()
    if k == "raw":
        return "WRITE " + v
    if k == "release":
        return "RELEASE " + v
    raise SystemExit(f"unknown command {item}")


def show(line: str) -> None:
    ts = time.strftime("%H:%M:%S")
    extra = ""
    parts = line.split()
    if parts and parts[0] == "NOTIFY" and len(parts) == 3:
        try:
            extra = f"   -> {p.parse_status(bytes.fromhex(parts[2]))}"
        except ValueError:
            pass
    print(f"{ts} < {line}{extra}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("items", nargs="*")
    ap.add_argument("--port", type=int, default=7700)
    ap.add_argument("--token", default="")
    ap.add_argument("--gap", type=float, default=1.5)
    ap.add_argument("--listen", type=float, default=3.0)
    a = ap.parse_args()
    s = socket.create_connection((a.host, a.port), timeout=5)
    s.settimeout(0.2)
    buf = b""

    def pump(seconds: float) -> None:
        nonlocal buf
        end = time.time() + seconds
        while time.time() < end:
            try:
                d = s.recv(4096)
                if not d:
                    print("connection closed")
                    sys.exit(1)
                buf += d
                while b"\n" in buf:
                    ln, buf = buf.split(b"\n", 1)
                    show(ln.decode())
            except socket.timeout:
                pass

    if a.token:
        s.sendall(f"AUTH {a.token}\n".encode())
    pump(0.5)
    for it in a.items:
        line = to_line(it)
        print(f"{time.strftime('%H:%M:%S')} > {line}", flush=True)
        s.sendall((line + "\n").encode())
        pump(a.gap)
    pump(a.listen)


if __name__ == "__main__":
    main()
