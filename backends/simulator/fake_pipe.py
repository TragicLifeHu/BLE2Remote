"""Fake blepipe + heater simulator for developing the app without hardware.

  python backends/simulator/fake_pipe.py [--port 7700] [--child-lock]
  python control_plane/server.py --pipe 127.0.0.1:7700 --http 127.0.0.1:8080 --data %TEMP%/heater-data

Simulates the verified device behaviour: status push on change, setpoint clears power-save,
setpoint/eco ignored while off, child lock ignores everything, pairing scan/pair/forget.
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "control_plane"))
import protocol as p  # noqa: E402

FAKE_ID = "00000000-0000-4000-8000-0000000000F1"


class Sim:
    def __init__(self, child_lock: bool):
        self.power, self.timer, self.mode, self.level, self.sp, self.voice = True, 0, 0, 0, 23, 0
        self.lock = child_lock
        self.paired = FAKE_ID
        self.state = "connected"
        self.clients: list[socket.socket] = []

    def frame(self) -> bytes:
        return p._wrap(bytes([0, 0x53, int(self.power), self.timer, self.mode, self.level, self.sp, self.voice]))

    def bcast(self, line: str) -> None:
        for c in list(self.clients):
            try:
                c.sendall((line + "\n").encode())
            except OSError:
                self.clients.remove(c)

    def push(self) -> None:
        self.bcast("NOTIFY FFB2 " + self.frame().hex())

    def apply(self, data: bytes) -> None:
        cmd, val = data[3], data[4]
        before = self.frame()
        if cmd == p.CMD_QUERY:
            return self.push()
        if self.lock or cmd == p.CMD_KEEPALIVE:
            return
        if cmd == p.CMD_POWER:
            self.power = bool(val)
            if not self.power:
                self.mode = self.level = 0
        elif cmd == p.CMD_SETPOINT and self.power:
            self.sp, self.mode, self.level = val, 0, 0
        elif cmd == p.CMD_ECO and self.power:
            self.mode, self.level = (3, 2) if val else (0, 0)
        elif cmd == p.CMD_TIMER:
            self.timer = val
        elif cmd == p.CMD_VOICE:
            self.voice = val
        if self.frame() != before:
            self.push()


def serve(sim: Sim, port: int) -> None:
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(4)
    print(f"fake pipe on 127.0.0.1:{port} (child lock {'ON' if sim.lock else 'off'})", flush=True)
    while True:
        c, _ = srv.accept()
        sim.clients.append(c)
        threading.Thread(target=handle, args=(sim, c), daemon=True).start()


def handle(sim: Sim, c: socket.socket) -> None:
    def send(s):
        c.sendall((s + "\n").encode())
    send("HELLO blepipe fake")
    send(f"PAIRED {sim.paired or '-'}")
    send(f"STATE {sim.state}")
    if sim.state == "connected":
        send(f"INFO {sim.paired} UAF028")
        send("RSSI -57")
    buf = b""
    while True:
        try:
            d = c.recv(4096)
        except OSError:
            return
        if not d:
            return
        buf += d
        while b"\n" in buf:
            ln, buf = buf.split(b"\n", 1)
            cmd, _, arg = ln.decode().strip().partition(" ")
            if cmd == "WRITE":
                send("OK")
                if sim.state == "connected":
                    sim.apply(bytes.fromhex(arg))
            elif cmd == "SCAN":
                send("OK")
                def scan():
                    time.sleep(1.5)
                    sim.bcast(f"DEVICE {FAKE_ID} -58 a50a11223344aabbccddeeff0300 UAF028")
                    time.sleep(1)
                    sim.bcast("DEVICE 11111111-2222-3333-4444-555555555555 -91 a50a556677881122334455660300 Uas-BLE")
                    time.sleep(2)
                    sim.bcast("SCANDONE")
                threading.Thread(target=scan, daemon=True).start()
            elif cmd == "PAIR":
                sim.paired, sim.state = arg, "connected"
                send("OK"); sim.bcast(f"PAIRED {arg}"); sim.bcast("STATE connected")
            elif cmd == "FORGET":
                sim.paired, sim.state = None, "unpaired"
                send("OK"); sim.bcast("PAIRED -"); sim.bcast("STATE unpaired")
            elif cmd == "RELEASE":
                sim.state = "released"; send("OK"); sim.bcast("STATE released")
                def back(s=float(arg or 60)):
                    time.sleep(s); sim.state = "connected"; sim.bcast("STATE connected")
                threading.Thread(target=back, daemon=True).start()
            elif cmd == "PING":
                send("PONG")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7700)
    ap.add_argument("--child-lock", action="store_true")
    a = ap.parse_args()
    serve(Sim(a.child_lock), a.port)
