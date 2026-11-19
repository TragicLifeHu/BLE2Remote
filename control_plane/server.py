"""Heater bridge app server: web app + REST API + scheduler (+ optional MQTT).

  python server.py --pipe 127.0.0.1:7700 --http 0.0.0.0:8080 --data ../data

API (JSON):
  GET  /api/state                      heater state, bridge state, clock, next event
  POST /api/control   {power,setpoint,eco,timer_hours,voice}
  GET  /api/schedule                   {rules, events, settings, upcoming}
  PUT  /api/schedule  {rules, events, settings}      (full replace, validated)
  POST /api/schedule/skip  {key, skip}
  GET  /api/pair                       scan results;  POST /api/pair/scan {seconds}
  POST /api/pair {id}  |  POST /api/pair/forget  |  POST /api/release {minutes}
  GET  /api/activity
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import mimetypes
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import controller as ctl  # noqa: E402
import heater as hz  # noqa: E402
import scheduler as sch  # noqa: E402
import store  # noqa: E402

VERSION = "1.1"


class OnTimerCheck(Exception):
    """An on-timer needs the user's decision: schedule events would cancel it, or nothing turns it off."""
    def __init__(self, info: dict):
        super().__init__("on-timer needs confirmation")
        self.info = info


class TimerOverlap(Exception):
    def __init__(self, overlaps: list[dict]):
        super().__init__("timer overlaps the schedule")
        self.overlaps = overlaps
STATIC = os.path.join(HERE, "static")
log = hz.log


class App:
    def __init__(self, args):
        self.args = args
        self.data = os.path.abspath(args.data)
        os.makedirs(self.data, exist_ok=True)
        self.f_sched = os.path.join(self.data, "schedule.json")
        self.f_state = os.path.join(self.data, "runner.json")
        self.f_act = os.path.join(self.data, "activity.json")
        self.f_runs = os.path.join(self.data, "runs.json")
        self.lock = threading.RLock()
        self.runs: list[dict] = store.load(self.f_runs, [])
        try:
            self.cfg = sch.validate(store.load(self.f_sched, sch.default_config()))
        except (ValueError, TypeError) as e:
            log("schedule file invalid, starting empty:", e)
            self.cfg = sch.default_config()
        st = store.load(self.f_state, {})
        last = dt.datetime.strptime(st["last"], sch.FMT) if st.get("last") else None
        self.runner = sch.Runner(last)
        # manual off-timer set with "schedule first": the first event that changes the heater cancels it
        self.manual_timer: dict | None = st.get("manual_timer")
        # pending on-timer armed from the app: {start, hours, off_after, set_at}; off_after = off-timer
        # hours the bridge sets once the heater has switched itself on (the heater sets none itself)
        self.on_timer: dict | None = st.get("on_timer")
        host, _, port = args.pipe.partition(":")
        f_dev = os.path.join(self.data, "last-paired.json")   # {id, name, mac} of the (last) paired heater
        self.heater = hz.Heater(host, int(port or 7700), args.pipe_token,
                                on_activity=lambda items: store.save(self.f_act, items),
                                device=store.load(f_dev, {}),
                                on_device=lambda d: store.save(f_dev, d))
        for item in reversed(store.load(self.f_act, [])[:100]):
            self.heater.activity.appendleft(item)
        self.safety_fired = False
        threading.Thread(target=self._sched_loop, daemon=True, name="scheduler").start()

    # ---------- clock ----------
    def now(self) -> dt.datetime:
        tz = self.cfg["settings"].get("timezone")
        if tz:
            try:
                from zoneinfo import ZoneInfo
                return dt.datetime.now(ZoneInfo(tz)).replace(tzinfo=None, microsecond=0)
            except Exception:  # noqa: BLE001 — bad zone name: fall back to system time
                pass
        return dt.datetime.now().replace(microsecond=0)

    def tz_name(self) -> str:
        return self.cfg["settings"].get("timezone") or time.strftime("%Z (UTC%z)")

    # ---------- schedule ----------
    def save_cfg(self) -> None:
        store.save(self.f_sched, self.cfg)

    def schedule_view(self) -> dict:
        with self.lock:
            now = self.now()
            return {**self.cfg, "upcoming": sch.upcoming(self.cfg, now, days=8, limit=30),
                    "now": now.strftime(sch.FMT), "paused": sch.paused(self.cfg, now),
                    "conflicts": sch.conflicts(self.cfg, now)}

    def set_schedule(self, new: dict) -> None:
        cfg = sch.validate(new)
        errors = [c for c in sch.conflicts(cfg, self.now()) if c["level"] == "error"]
        if errors:
            raise ValueError(errors[0]["text"])
        with self.lock:
            self.cfg = sch.prune(cfg, self.now())
            self.save_cfg()
        self.heater.record("schedule", "Schedule updated")

    def skip(self, key: str, on: bool) -> None:
        with self.lock:
            skips = set(self.cfg["settings"]["skips"])
            (skips.add if on else skips.discard)(key)
            self.cfg["settings"]["skips"] = sorted(skips)
            self.save_cfg()

    def _save_state(self) -> None:
        last = self.runner.last.strftime(sch.FMT) if self.runner.last else None
        store.save(self.f_state, {"last": last, "manual_timer": self.manual_timer,
                                     "on_timer": self.on_timer})

    # ---------- manual off-timer vs schedule ----------
    def timer_overlaps(self, hours: int) -> list[dict]:
        with self.lock:
            now = self.now()
            return [{"key": o["key"], "at": o["at"], "name": o["name"], "action": sch.describe(o["action"])}
                    for o in sch.within(self.cfg, now, hours)]

    def set_manual_timer(self, changes: dict, mode: str | None, off_after: int = 0) -> None:
        """Apply a manual control request containing timer_hours, honouring the overlap choice:
        mode 'timer'    -> skip the overlapping events (not restored if the timer is cancelled later)
        mode 'schedule' -> events run; the first that changes the heater cancels the timer."""
        hours = int(changes["timer_hours"])
        cur = self.heater.status_now()
        if cur is not None and not cur.power and changes.get("power") is not True:
            return self._set_on_timer(changes, hours, mode, off_after)
        overlaps = self.timer_overlaps(hours) if hours else []
        if overlaps and mode not in ("timer", "schedule"):
            raise TimerOverlap(overlaps)
        self.heater.submit(changes, source="app")
        with self.lock:
            if not hours:
                self.manual_timer = None
            elif mode == "timer" and overlaps:
                skips = set(self.cfg["settings"]["skips"]) | {o["key"] for o in overlaps}
                self.cfg["settings"]["skips"] = sorted(skips)
                self.save_cfg()
                self.manual_timer = None
            elif mode == "schedule" and overlaps:
                until = self.now() + dt.timedelta(hours=hours)
                self.manual_timer = {"until": until.strftime(sch.FMT), "hours": hours, "set_at": time.time()}
            else:
                self.manual_timer = None
            self._save_state()
        if mode == "timer" and overlaps:
            names = ", ".join(f"{o['name'] or o['action']} {o['at'][11:]}" for o in overlaps)
            self.heater.record("schedule", f"Skipped for the {hours} h timer: {names}")
        elif mode == "schedule" and overlaps:
            self.heater.record("schedule", f"{hours} h timer set — the next schedule change will cancel it")

    def _set_on_timer(self, changes: dict, hours: int, mode: str | None, off_after: int) -> None:
        """Heater off: the timer is an on-timer. Any power-on before it fires cancels it, and once it
        fires the heater has no off-timer, so ask first when the schedule interferes or nothing ends it.
        mode: None (check) | 'ok' (go ahead) | 'timer' (skip the events that would cancel it)
              | 'schedule' (let them run; the first one cancels the on-timer)."""
        if not 0 <= off_after <= 24:
            raise ValueError("off_after must be 0..24")
        with self.lock:
            info = sch.on_timer_check(self.cfg, self.now(), hours) if hours else None
            long_h = self.cfg["settings"].get("max_on_hours") or 12   # a run longer than this needs a look
        if info:
            info["long_h"] = long_h
            info["long"] = info["off"] is None or info["off"]["runs_h"] > long_h
        if info and mode not in ("ok", "timer", "schedule") and (info["cancels"] or info["long"]):
            raise OnTimerCheck(info)
        self.heater.submit(changes, source="app")
        with self.lock:
            self.manual_timer = None
            self.on_timer = None
            if info:
                self.on_timer = {"start": info["start"], "hours": hours, "off_after": off_after,
                                 "set_at": time.time()}
                if mode == "timer" and info["cancels"]:
                    skips = set(self.cfg["settings"]["skips"]) | {c["key"] for c in info["cancels"]}
                    self.cfg["settings"]["skips"] = sorted(skips)
                    self.save_cfg()
            self._save_state()
        if info and mode == "timer" and info["cancels"]:
            names = ", ".join(f"{c['name'] or c['action']} {c['at'][11:]}" for c in info["cancels"])
            self.heater.record("schedule", f"Skipped for the on-timer: {names}")
        if info and off_after:
            self.heater.record("schedule", f"On-timer starts around {info['start'][11:]}; "
                                           f"the bridge will set a {off_after} h off-timer then")

    def _track_on_timer(self, cur, now: dt.datetime) -> None:
        """Follow a pending on-timer: when it fires set the follow-up off-timer, else notice it is gone."""
        ot = self.on_timer
        if not ot or cur is None or time.time() - ot.get("set_at", 0) < 20:   # wait for the heater to confirm
            return
        start = dt.datetime.strptime(ot["start"], sch.FMT)
        near_start = now >= start - dt.timedelta(minutes=5)
        if not cur.power and cur.timer_hours:
            return                                                  # still pending
        with self.lock:
            self.on_timer = None
            self._save_state()
        if cur.power and near_start:
            if ot.get("off_after"):
                try:
                    self.heater.submit({"timer_hours": ot["off_after"]}, source="schedule · after on-timer")
                except ValueError as e:
                    self.heater.record("error", f"Off-timer after the on-timer not set: {e}")
        elif cur.power:
            self.heater.record("timer", "Heater was turned on before its on-timer — on-timer cancelled")
        elif near_start:
            self.heater.record("error", "On-timer ran out but the heater didn't switch on")

    def _sched_loop(self) -> None:
        saved_at = 0.0
        while True:
            try:
                self._sched_tick()
                if time.time() - saved_at > 60:
                    self._save_state()
                    saved_at = time.time()
            except Exception as e:  # noqa: BLE001
                log("scheduler error", repr(e))
            time.sleep(10)

    # ---------- run history (7 days) ----------
    RUN_DAYS = 7

    def _save_runs(self) -> None:
        cutoff = (self.now() - dt.timedelta(days=self.RUN_DAYS)).strftime(sch.FMT)
        with self.lock:
            self.runs = [r for r in self.runs if r["at"] >= cutoff][-500:]
            store.save(self.f_runs, self.runs)

    def _new_run(self, o: dict, result: str, detail: str = "") -> dict:
        now = self.now()
        at = o["at"] if isinstance(o["at"], dt.datetime) else dt.datetime.strptime(o["at"], sch.FMT)
        run = {"key": o["key"], "kind": o.get("kind", "rule"), "name": o.get("name") or "",
               "action": sch.describe(o["action"]) if "action" in o else o.get("action_text", ""),
               "at": at.strftime(sch.FMT), "ran": now.strftime("%Y-%m-%dT%H:%M:%S"),
               "delay_s": max(0, int((now - at).total_seconds())), "result": result, "detail": detail}
        with self.lock:
            self.runs.append(run)
        self._save_runs()
        return run

    def _finish_run(self, run: dict, res: dict) -> None:
        with self.lock:
            run.update({k: res.get(k) for k in ("result", "detail", "before", "after", "commands", "duration_s")})
        self._save_runs()

    def already_ran(self, key: str) -> bool:
        with self.lock:
            return any(r["key"] == key and r["result"] not in ("skipped", "paused") for r in self.runs)

    def _sched_tick(self) -> None:
        with self.lock:
            now = self.now()
            cfg = sch.prune(self.cfg, now)
            fired = self.runner.tick(cfg, now, self.heater.available())
            notes = list(self.runner.notes)
            settings = dict(cfg["settings"])
        for n in notes:
            if not any(r["key"] == n["key"] and r["result"] == n["outcome"] for r in self.runs):
                detail = {"skipped": "skipped once by you", "paused": "schedules paused",
                          "missed": "heater unreachable for the whole catch-up window",
                          "superseded": "a later event replaced it while catching up"}[n["outcome"]]
                self._new_run(n, n["outcome"], detail)
        # a "schedule first" manual timer is over once the heater's timer reads 0 (expired / cancelled)
        cur = self.heater.status_now()
        if (self.manual_timer and cur is not None and cur.timer_hours == 0
                and time.time() - self.manual_timer.get("set_at", 0) > 20):   # not before the heater confirmed it
            self.manual_timer = None
            self._save_state()
        self._track_on_timer(cur, now)
        for o in fired:
            if self.already_ran(o["key"]):           # e.g. restart right after an event
                log("skip duplicate run", o["key"])
                continue
            self._save_state()
            ch = sch.action_changes(o["action"], settings)
            label = o["name"] or sch.describe(o["action"])
            if self.manual_timer and "timer_hours" not in ch and cur is not None:
                try:
                    changes_state = bool(ctl.plan(cur, ch).frames)
                except ValueError:
                    changes_state = False
                if changes_state:                     # schedule first: this change cuts the manual timer
                    ch["timer_hours"] = 0
                    self.manual_timer = None
                    self._save_state()
                    self.heater.record("schedule", f"“{label}” changed the heater — manual off-timer cancelled")
            run = self._new_run(o, "running")
            try:
                self.heater.submit(ch, source=f"schedule · {label}", on_done=lambda res, r=run: self._finish_run(r, res))
            except ValueError as e:
                self.heater.record("error", f"Schedule '{label}' not applied: {e}")
                self._finish_run(run, {"result": "error", "detail": str(e)})
        # safety: maximum continuous on-time
        max_h = settings.get("max_on_hours") or 0
        st = self.heater.state()
        if max_h and st.get("power") and st.get("on_since"):
            if time.time() - st["on_since"] >= max_h * 3600 and not self.safety_fired:
                self.safety_fired = True
                run = self._new_run({"key": f"safety:{int(time.time())}", "kind": "safety", "name": "Safety limit",
                                     "action": {"power": False}, "at": now}, "running")
                try:
                    self.heater.submit({"power": False}, source=f"safety · on for {max_h} h",
                                       on_done=lambda res, r=run: self._finish_run(r, res))
                except ValueError as e:
                    self.heater.record("error", f"Safety off failed: {e}")
                    self._finish_run(run, {"result": "error", "detail": str(e)})
        elif not st.get("power"):
            self.safety_fired = False

    # ---------- views ----------
    def state_view(self) -> dict:
        s = self.heater.state()
        with self.lock:
            now = self.now()
            nxt = [u for u in sch.upcoming(self.cfg, now, days=8, limit=10) if not u["skipped"] and not u["paused"]]
            s["next"] = nxt[0] if nxt else None
            if s["next"]:
                ch = sch.action_changes(s["next"]["action"], self.cfg["settings"])
                s["next"]["preview"] = ctl.preview(self.heater.status_now(), ch)
            s["paused_until"] = self.cfg["settings"]["pause_until"] if sch.paused(self.cfg, now) else None
            s["max_on_hours"] = self.cfg["settings"]["max_on_hours"]
            s["manual_timer"] = self.manual_timer
            s["on_timer"] = self.on_timer
        s["clock"] = {"now": now.strftime("%Y-%m-%dT%H:%M:%S"), "tz": self.tz_name()}
        s["version"] = VERSION
        return s


def make_handler(app: App):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _body(self) -> dict:
            n = min(int(self.headers.get("Content-Length", 0) or 0), 64 * 1024)
            data = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("body must be a JSON object")
            return data

        def _static(self, path: str) -> None:
            rel = "index.html" if path in ("", "/") else path.lstrip("/")
            full = os.path.normpath(os.path.join(STATIC, rel))
            if not full.startswith(STATIC) or not os.path.isfile(full):
                full = os.path.join(STATIC, "index.html")   # SPA fallback
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if full.endswith(".webmanifest"):
                ctype = "application/manifest+json"
            if ctype.startswith("text/") or ctype.endswith("javascript") or ctype.endswith("json"):
                ctype += "; charset=utf-8"
            with open(full, "rb") as f:
                self._send(200, f.read(), ctype, "no-cache")

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/state":
                return self._json(200, app.state_view())
            if path == "/api/schedule":
                return self._json(200, app.schedule_view())
            if path == "/api/pair":
                return self._json(200, app.heater.scan_result())
            if path == "/api/activity":
                return self._json(200, list(app.heater.activity))
            if path == "/api/runs":
                with app.lock:
                    return self._json(200, list(reversed(app.runs)))
            if path.startswith("/api/"):
                return self._json(404, {"error": "not found"})
            self._static(path)

        def do_PUT(self):
            self._mutate("PUT")

        def do_POST(self):
            self._mutate("POST")

        def _mutate(self, method: str):
            path = self.path.split("?", 1)[0]
            try:
                body = self._body()
                if method == "POST" and path == "/api/control":
                    mode = body.pop("timer_mode", None)
                    off_after = int(body.pop("off_after", 0) or 0)
                    ch = ctl.normalize(body)
                    if "timer_hours" in ch:
                        try:
                            app.set_manual_timer(ch, mode, off_after)
                        except OnTimerCheck as t:
                            return self._json(409, {"error": "on-timer needs confirmation", "on_timer": t.info})
                        except TimerOverlap as t:
                            return self._json(409, {"error": "timer overlaps the schedule",
                                                    "overlaps": t.overlaps})
                    else:
                        app.heater.submit(ch, source="app")
                elif method == "PUT" and path == "/api/schedule":
                    app.set_schedule(body)
                    return self._json(200, app.schedule_view())
                elif method == "POST" and path == "/api/schedule/skip":
                    app.skip(str(body["key"]), bool(body.get("skip", True)))
                    return self._json(200, app.schedule_view())
                elif method == "POST" and path == "/api/pair/scan":
                    app.heater.start_scan(int(body.get("seconds", 15)))
                elif method == "POST" and path == "/api/pair":
                    app.heater.pair(str(body["id"]))
                elif method == "POST" and path == "/api/pair/forget":
                    app.heater.forget()
                elif method == "POST" and path == "/api/release":
                    # minutes=0 -> 1 s, i.e. "reconnect now"
                    app.heater.release(max(1, min(int(float(body.get("minutes", 10)) * 60), 3600)))
                else:
                    return self._json(404, {"error": "not found"})
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
                return self._json(400, {"error": str(e).strip("'\"")})
            self._json(200, {"ok": True})

    return H


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pipe", default="127.0.0.1:7700")
    ap.add_argument("--pipe-token", default="")
    ap.add_argument("--http", default="0.0.0.0:8080")
    ap.add_argument("--data", default=os.path.join(HERE, "..", "data"))
    ap.add_argument("--mqtt", default="", help="optional broker host:port (see docs/roadmap.md)")
    ap.add_argument("--mqtt-user", default="")
    ap.add_argument("--mqtt-pass", default=os.environ.get("HEATER_MQTT_PASS", ""))
    ap.add_argument("--device-id", default="heater")
    args = ap.parse_args()

    app = App(args)
    if args.mqtt:
        import mqtt_bridge
        mqtt_bridge.start(app.heater, args, VERSION)
    h, _, port = args.http.rpartition(":")

    class Server(ThreadingHTTPServer):
        def server_bind(self):
            # HTTPServer.server_bind() calls socket.getfqdn(), a reverse-DNS lookup that can stall for
            # ~60 s on the iPhone. We don't need the hostname.
            import socketserver
            socketserver.TCPServer.server_bind(self)
            self.server_name, self.server_port = "bridge", self.server_address[1]

    srv = Server((h or "0.0.0.0", int(port)), make_handler(app))
    srv.daemon_threads = True
    log(f"heater bridge {VERSION} on http://{h or '0.0.0.0'}:{port}  data={app.data}")
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
