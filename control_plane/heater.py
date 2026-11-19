"""Heater link: blepipe client, state cache, command worker with verification, pairing, activity log."""
from __future__ import annotations

import collections
import dataclasses
import queue
import socket
import threading
import time

import controller as ctl
import protocol as p


LINK_GRACE_S = 90   # link drops shorter than this are not written to the activity log


def log(*a) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def duration(s: float) -> str:
    m = int(s // 60)
    if m < 1:
        return f"{int(s)} s"
    if m < 60:
        return f"{m} min"
    return f"{m // 60} h {m % 60} min" if m % 60 else f"{m // 60} h"


def mac_from_mfr(mfr_hex: str) -> str | None:
    """AirMeter adverts: company 0x0AA5 (a5 0a) + 4 bytes + MAC(6) + 2 bytes."""
    try:
        b = bytes.fromhex(mfr_hex)
    except ValueError:
        return None
    if len(b) >= 12 and b[:2] == b"\xa5\x0a":
        return ":".join(f"{x:02X}" for x in b[6:12])
    return None


class Heater:
    def __init__(self, host: str, port: int, token: str = "", on_activity=None,
                 device: dict | None = None, on_device=None):
        # info about the paired / last paired heater: {id, name, mac}; kept across "Forget"
        self.device: dict = dict(device or {})
        self.last_paired = self.device.get("id")
        self.on_device = on_device
        self.rssi: int | None = None
        self.connected_since: float | None = None
        self.host, self.port, self.token = host, port, token
        self.cv = threading.Condition()
        self.status: p.Status | None = None
        self.ble = "disconnected"         # pipe state, or "disconnected" if the pipe is unreachable
        self.paired: str | None = None
        self.rejected = False
        self.last_seen: float | None = None
        self.on_since: float | None = None
        self.scan: dict = {"active": False, "devices": {}}
        self.sock: socket.socket | None = None
        self.listeners: list = []
        self.activity = collections.deque(maxlen=100)
        self.on_activity = on_activity
        self.cmds: queue.Queue = queue.Queue()
        self._expect_change_until = 0.0   # status changes before this came from us
        self._lost_at: float | None = None   # when the link dropped (None while connected)
        self._lost_logged = False            # the drop outlasted LINK_GRACE_S and was logged
        self._ever_connected = False
        threading.Thread(target=self._link_loop, daemon=True, name="pipe").start()
        threading.Thread(target=self._worker, daemon=True, name="worker").start()

    # ---------------- public ----------------
    def available(self) -> bool:
        return self.ble == "connected" and self.status is not None

    def state(self) -> dict:
        with self.cv:
            s = ctl.to_state(self.status, self.ble, self.rejected)
            s.update(paired=self.paired, last_seen=self.last_seen, on_since=self.on_since)
            dev = self.device if self.paired and self.device.get("id") == self.paired else {"id": self.paired}
            s["device"] = {**dev, "rssi": self.rssi, "connected_since": self.connected_since} if self.paired else None
            return s

    def _save_device(self) -> None:
        if self.on_device:
            self.on_device(dict(self.device))

    def submit(self, changes: dict, source: str = "app", on_done=None) -> None:
        """Validate now (raises ValueError), apply asynchronously.
        on_done(result) gets {result: applied|noop|rejected|error, detail, before, after, commands, duration_s}."""
        with self.cv:
            cur = self.status
        if cur is None or self.ble != "connected":
            raise ValueError("heater is not connected")
        ctl.plan(cur, changes)
        self.cmds.put((changes, source, on_done))

    def status_now(self) -> p.Status | None:
        with self.cv:
            return self.status if self.ble == "connected" else None

    def record(self, kind: str, text: str) -> None:
        item = {"t": time.time(), "kind": kind, "text": text}
        self.activity.appendleft(item)
        log("activity", kind, text)
        if self.on_activity:
            self.on_activity(list(self.activity))

    def on_change(self, fn) -> None:
        self.listeners.append(fn)

    # pairing / pipe control
    def start_scan(self, seconds: int = 15) -> None:
        with self.cv:
            self.scan = {"active": True, "devices": {}}
        if not self._send(f"SCAN {seconds}"):
            raise ValueError("bridge pipe is not reachable")

    def pair(self, ident: str) -> None:
        if not self._send(f"PAIR {ident}"):
            raise ValueError("bridge pipe is not reachable")
        self.record("pair", f"Paired heater {ident[:8]}…")

    def forget(self) -> None:
        if not self._send("FORGET"):
            raise ValueError("bridge pipe is not reachable")
        self.record("pair", "Forgot heater")

    def release(self, seconds: int) -> None:
        if not self._send(f"RELEASE {seconds}"):
            raise ValueError("bridge pipe is not reachable")
        self.record("bridge", f"Released Bluetooth for {seconds // 60} min")

    def scan_result(self) -> dict:
        with self.cv:
            devs = sorted(self.scan["devices"].values(), key=lambda d: (not d["previously_paired"], -d["rssi"]))
            return {"active": self.scan["active"], "devices": devs, "paired": self.paired,
                    "last_paired": self.last_paired}

    # ---------------- internals ----------------
    def _notify(self) -> None:
        st = self.state()
        for fn in self.listeners:
            try:
                fn(st)
            except Exception as e:  # noqa: BLE001
                log("listener error", e)

    def _send(self, line: str) -> bool:
        s = self.sock
        if s is None:
            return False
        try:
            s.sendall((line + "\n").encode())
            return True
        except OSError:
            return False

    def _link_loop(self) -> None:
        backoff = 1.0
        while True:
            try:
                s = socket.create_connection((self.host, self.port), timeout=10)
                s.settimeout(None)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                self.sock = s
                log(f"pipe connected {self.host}:{self.port}")
                backoff = 1.0
                if self.token:
                    self._send(f"AUTH {self.token}")
                buf = b""
                while True:
                    d = s.recv(4096)
                    if not d:
                        break
                    buf += d
                    while b"\n" in buf:
                        ln, buf = buf.split(b"\n", 1)
                        self._on_line(ln.decode("utf-8", "replace").strip())
            except OSError as e:
                log("pipe error:", e)
            self.sock = None
            self._set_ble("disconnected")
            self._notify()
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)

    def _set_ble(self, new: str) -> None:
        """Track the link state. Brief drops (the heater or iOS can blip the link) stay out of the activity
        log: a loss is only recorded once it has lasted LINK_GRACE_S, and the reconnect then says for how long."""
        now = time.time()
        with self.cv:
            prev, self.ble = self.ble, new
            if new == prev:
                return
            if new == "connected":
                was_lost, lost_at = self._lost_logged, self._lost_at
                self._lost_at, self._lost_logged = None, False
                if self.connected_since is None or was_lost:
                    self.connected_since = now
                first = not self._ever_connected
                self._ever_connected = True
            elif prev == "connected":
                self._lost_at = now
                self.rssi = None
            self.cv.notify_all()
        log("ble", new)
        if new == "connected":
            if first:
                self.record("link", "Heater connected")
            elif was_lost:
                self.record("link", f"Heater reconnected after {duration(now - lost_at)}")
        elif prev == "connected":
            t = threading.Timer(LINK_GRACE_S, self._check_lost, args=(now,))
            t.daemon = True
            t.start()

    def _check_lost(self, lost_at: float) -> None:
        with self.cv:
            if self.ble == "connected" or self._lost_at != lost_at:
                return
            self._lost_logged = True
            self.connected_since = None
            ble = self.ble
        self.record("link", f"Heater link lost ({ble})")
        self._notify()

    def _on_line(self, line: str) -> None:
        parts = line.split(" ")
        tag = parts[0]
        if tag == "STATE" and len(parts) > 1:
            self._set_ble(parts[1])
            if self.ble == "connected":
                self._send("WRITE " + p.query().hex())
            self._notify()
        elif tag == "PAIRED" and len(parts) > 1:
            with self.cv:
                self.paired = None if parts[1] == "-" else parts[1]
                if self.paired is None:
                    self.status = None
                elif self.paired != self.last_paired:
                    self.last_paired = self.paired          # remembered even after "Forget"
                    found = self.scan["devices"].get(self.paired, {})
                    self.device = {"id": self.paired, "name": found.get("name"), "mac": found.get("mac")}
                    self._save_device()
            self._notify()
        elif tag == "NOTIFY" and len(parts) == 3 and parts[1].upper() == "FFB2":
            try:
                st = p.parse_status(bytes.fromhex(parts[2]))
            except ValueError as e:
                log("bad frame", e)
                return
            self._on_status(st)
        elif tag == "DEVICE" and len(parts) >= 5:
            ident, rssi, mfr, name = parts[1], int(parts[2]), parts[3], " ".join(parts[4:])
            with self.cv:
                self.scan["devices"][ident] = {
                    "id": ident, "rssi": rssi, "name": None if name == "-" else name,
                    "mac": mac_from_mfr(mfr) if mfr != "-" else None,
                    # Only a hint: manufacturer ID 0x0AA5 belongs to the BLE module, not to a product brand.
                    "same_module": mfr.startswith("a50a"),
                    "previously_paired": ident == self.last_paired,
                }
        elif tag == "INFO" and len(parts) >= 3:
            name = " ".join(parts[2:])
            with self.cv:
                if parts[1] == self.paired and name != "-" and self.device.get("name") != name:
                    self.device["name"] = name
                    self._save_device()
            self._notify()
        elif tag == "RSSI" and len(parts) == 2:
            try:
                with self.cv:
                    self.rssi = int(parts[1])
            except ValueError:
                pass
        elif tag == "SCANDONE":
            with self.cv:
                self.scan["active"] = False
        elif tag == "ERR":
            log("pipe:", line)

    def _on_status(self, st: p.Status) -> None:
        now = time.time()
        with self.cv:
            prev = self.status
            changed = st != prev
            self.status = st
            self.last_seen = now
            if st.power and (prev is None or not prev.power):
                self.on_since = now
            elif not st.power:
                self.on_since = None
            external = changed and prev is not None and now > self._expect_change_until
            if changed and self.rejected:
                self.rejected = False      # something changed again -> lock evidently not blocking
            self.cv.notify_all()
        if changed:
            log("status", st)
            if external:
                countdown = (dataclasses.replace(prev, timer_hours=st.timer_hours) == st
                             and st.timer_hours == prev.timer_hours - 1)
                if prev.timer_hours == 1 and prev.power and not st.power and st.timer_hours == 0:
                    self.record("timer", "Heater's off-timer finished")
                elif prev.timer_hours == 1 and not prev.power and st.power and st.timer_hours == 0:
                    self.record("timer", "Heater's on-timer switched it on")
                elif not countdown:
                    self.record("manual", "Changed on the heater / remote: " + describe_diff(prev, st))
            self._notify()

    def _wait_for(self, expect: dict, timeout: float) -> bool:
        end = time.time() + timeout
        with self.cv:
            while not ctl.satisfied(self.status, expect):
                left = end - time.time()
                if left <= 0:
                    return False
                self.cv.wait(left)
            return True

    def _worker(self) -> None:
        while True:
            changes, source, on_done = self.cmds.get()
            try:
                res = self._apply(changes, source)
            except Exception as e:  # noqa: BLE001
                res = {"result": "error", "detail": repr(e)}
            if on_done:
                try:
                    on_done(res)
                except Exception as e:  # noqa: BLE001
                    log("on_done error", e)

    def _apply(self, changes: dict, source: str) -> dict:
        with self.cv:
            cur = self.status
        before = ctl.summarize(cur)
        if cur is None or self.ble != "connected":
            self.record("error", f"Skipped ({source}): heater not connected")
            return {"result": "error", "detail": "heater not connected", "before": before}
        try:
            plan = ctl.plan(cur, changes)
        except ValueError as e:
            self.record("error", f"Skipped ({source}): {e}")
            return {"result": "error", "detail": str(e), "before": before}
        if not plan.frames:
            if source != "app":
                self.record(source, f"Nothing to do — already {before}")
            return {"result": "noop", "detail": f"already {before}", "before": before, "after": before,
                    "commands": 0, "duration_s": 0.0}
        t0 = time.time()
        self._expect_change_until = t0 + 6 + sum(plan.delays)
        for f, pause in zip(plan.frames, plan.delays):
            self._send("WRITE " + f.hex())
            time.sleep(pause)
        ok = self._wait_for(plan.expect, 2.5)
        if not ok:
            self._send("WRITE " + p.query().hex())
            ok = self._wait_for(plan.expect, 1.5)
        duration = round(time.time() - t0, 1)
        with self.cv:
            self.rejected = not ok
            after = ctl.summarize(self.status)
        what = describe_changes(changes, on=cur.power or changes.get("power") is True)
        if ok:
            self.record(source, what)
        else:
            self.record("rejected", f"Heater ignored: {what} — child lock on?")
        self._notify()
        return {"result": "applied" if ok else "rejected",
                "detail": what if ok else "heater ignored the command — child lock on?",
                "before": before, "after": after, "commands": len(plan.frames), "duration_s": duration}


def describe_changes(ch: dict, on: bool = True) -> str:
    """`on`: the heater is (or will be) on, so a timer is an off-timer; otherwise an on-timer."""
    parts = []
    if "power" in ch:
        parts.append("on" if ctl.normalize({"power": ch["power"]})["power"] else "off")
    if "setpoint" in ch:
        parts.append(f"{ctl.normalize({'setpoint': ch['setpoint']})['setpoint']}°C")
    if "eco" in ch:
        parts.append("power-save " + ("on" if ctl.normalize({"eco": ch["eco"]})["eco"] else "off"))
    if "timer_hours" in ch:
        h = ctl.normalize({"timer_hours": ch["timer_hours"]})["timer_hours"]
        kind = "off-timer" if on else "on-timer"
        parts.append(f"{kind} {h} h" if h else f"{kind} cancelled")
    if "voice" in ch:
        parts.append("voice " + ("on" if ctl.normalize({"voice": ch["voice"]})["voice"] else "off"))
    return ", ".join(parts) or "no change"


def describe_diff(a: p.Status | None, b: p.Status) -> str:
    if a is None:
        return "state received"
    out = []
    if a.power != b.power:
        out.append("on" if b.power else "off")
    if a.setpoint != b.setpoint and b.setpoint:
        out.append(f"{b.setpoint}°C")
    if a.eco != b.eco:
        out.append("power-save " + ("on" if b.eco else "off"))
    if a.timer_hours != b.timer_hours:
        kind = "off-timer" if b.power else "on-timer"
        out.append(f"{kind} {b.timer_hours} h" if b.timer_hours else f"{kind} cancelled")
    if a.voice != b.voice:
        out.append("voice " + ("on" if b.voice else "off"))
    return ", ".join(out) or "minor change"
