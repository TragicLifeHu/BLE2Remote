"""Wall-clock scheduler for the heater (pure logic, no I/O, no threads).

Model (persisted as JSON by the server):
  rules   : weekly rules  {id, name, enabled, days:[0..6 Mon=0], time:"HH:MM", action}
  events  : one-off       {id, name, enabled, at:"YYYY-MM-DDTHH:MM", action}
  action  : {"power": bool, "setpoint": int|None, "eco": bool, "hours": 0..24 (heater off-timer, 0 = untouched)}
  settings: pause_until ("YYYY-MM-DDTHH:MM" or None), skips [occurrence keys],
            catchup_minutes, max_on_hours (0 = off; bridge-side check), timezone ("" = system)

Semantics:
  * Edge-triggered: each occurrence fires exactly once at its time. The scheduler never re-asserts
    state, so manual / remote changes hold until the next scheduled event ("manual override hold").
  * Missed occurrences (bridge down, heater unplugged) are caught up when possible, but only the
    latest one, and only if it is younger than catchup_minutes.
  * Skips: an occurrence key in `skips` does not fire. Paused: nothing fires before pause_until.
"""
from __future__ import annotations

import datetime as dt
import uuid

import protocol as p

FMT = "%Y-%m-%dT%H:%M"
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

DEFAULT_SETTINGS = {
    "pause_until": None,
    "skips": [],
    "catchup_minutes": 30,
    "max_on_hours": 0,
    "timezone": "",
}


def default_config() -> dict:
    return {"rules": [], "events": [], "settings": dict(DEFAULT_SETTINGS)}


def new_id() -> str:
    return uuid.uuid4().hex[:8]


# ---------------- validation ----------------
def _action(a) -> dict:
    if not isinstance(a, dict):
        raise ValueError("action must be an object")
    power = bool(a.get("power"))
    out = {"power": power, "setpoint": None, "eco": False, "hours": 0}
    if power:
        sp = a.get("setpoint")
        if sp is not None:
            sp = int(sp)
            if not p.TEMP_MIN <= sp <= p.TEMP_MAX:
                raise ValueError(f"setpoint must be {p.TEMP_MIN}..{p.TEMP_MAX}")
        out["setpoint"] = sp
        out["eco"] = bool(a.get("eco"))
        h = int(a.get("hours") or 0)
        if not 0 <= h <= p.TIMER_MAX:
            raise ValueError(f"duration must be 0..{p.TIMER_MAX} hours")
        out["hours"] = h   # 0 = until the next event; N = heater's own off-timer
    return out


def _hhmm(s) -> str:
    t = dt.datetime.strptime(str(s), "%H:%M")
    return t.strftime("%H:%M")


def validate(cfg: dict) -> dict:
    """Return a cleaned copy of a schedule config; raise ValueError on bad input."""
    if not isinstance(cfg, dict):
        raise ValueError("config must be an object")
    rules = []
    for r in cfg.get("rules", []):
        days = sorted({int(d) for d in r.get("days", [])})
        if not days or any(d < 0 or d > 6 for d in days):
            raise ValueError("rule needs at least one day (0=Mon..6=Sun)")
        rules.append({
            "id": str(r.get("id") or new_id()),
            "name": str(r.get("name", ""))[:40],
            "enabled": bool(r.get("enabled", True)),
            "days": days,
            "time": _hhmm(r.get("time")),
            "action": _action(r.get("action")),
        })
    events = []
    for e in cfg.get("events", []):
        at = dt.datetime.strptime(str(e.get("at")), FMT).strftime(FMT)
        events.append({
            "id": str(e.get("id") or new_id()),
            "name": str(e.get("name", ""))[:40],
            "enabled": bool(e.get("enabled", True)),
            "at": at,
            "action": _action(e.get("action")),
        })
    s_in = cfg.get("settings", {}) or {}
    s = dict(DEFAULT_SETTINGS)
    pu = s_in.get("pause_until")
    s["pause_until"] = dt.datetime.strptime(pu, FMT).strftime(FMT) if pu else None
    s["skips"] = [str(k) for k in s_in.get("skips", [])][:200]
    s["catchup_minutes"] = max(0, min(int(s_in.get("catchup_minutes", 30)), 240))
    s["max_on_hours"] = max(0, min(int(s_in.get("max_on_hours", 0)), 24))
    s["timezone"] = str(s_in.get("timezone", "") or "")
    return {"rules": rules, "events": events, "settings": s}


# ---------------- occurrences ----------------
def occurrences(cfg: dict, start: dt.datetime, end: dt.datetime) -> list[dict]:
    """All enabled occurrences with start < at <= end, sorted by time (naive local datetimes)."""
    out = []
    day = start.date()
    while day <= end.date():
        for r in cfg["rules"]:
            if not r["enabled"] or day.weekday() not in r["days"]:
                continue
            h, m = map(int, r["time"].split(":"))
            at = dt.datetime.combine(day, dt.time(h, m))
            if start < at <= end:
                out.append({"key": f"r:{r['id']}:{at.strftime(FMT)}", "at": at, "name": r["name"],
                            "action": r["action"], "kind": "rule", "id": r["id"]})
        day += dt.timedelta(days=1)
    for e in cfg["events"]:
        if not e["enabled"]:
            continue
        at = dt.datetime.strptime(e["at"], FMT)
        if start < at <= end:
            out.append({"key": f"e:{e['id']}", "at": at, "name": e["name"], "action": e["action"],
                        "kind": "event", "id": e["id"]})
    out.sort(key=lambda o: o["at"])
    return out


def paused(cfg: dict, now: dt.datetime) -> bool:
    pu = cfg["settings"].get("pause_until")
    return bool(pu) and now < dt.datetime.strptime(pu, FMT)


def due(cfg: dict, last: dt.datetime, now: dt.datetime) -> list[dict]:
    """Occurrences in (last, now] that should fire (not skipped, not paused at their time)."""
    skips = set(cfg["settings"].get("skips", []))
    return [o for o in occurrences(cfg, last, now) if o["key"] not in skips and not paused(cfg, o["at"])]


def upcoming(cfg: dict, now: dt.datetime, days: int = 7, limit: int = 20) -> list[dict]:
    skips = set(cfg["settings"].get("skips", []))
    res = []
    for o in occurrences(cfg, now, now + dt.timedelta(days=days)):
        res.append({**o, "at": o["at"].strftime(FMT), "skipped": o["key"] in skips,
                    "paused": paused(cfg, o["at"])})
        if len(res) >= limit:
            break
    return res


def prune(cfg: dict, now: dt.datetime) -> dict:
    """Drop past one-off events (> 1 day old), stale skips and an expired pause."""
    cutoff = now - dt.timedelta(days=1)
    cfg["events"] = [e for e in cfg["events"] if dt.datetime.strptime(e["at"], FMT) > cutoff]
    keep = []
    for k in cfg["settings"]["skips"]:
        try:   # rule keys look like "r:<id>:<YYYY-MM-DDTHH:MM>"
            at = dt.datetime.strptime(k.split(":", 2)[2], FMT) if k.startswith("r:") else None
        except (ValueError, IndexError):
            at = None
        if at is None or at > cutoff:
            keep.append(k)
    cfg["settings"]["skips"] = keep
    if cfg["settings"]["pause_until"] and not paused(cfg, now):
        cfg["settings"]["pause_until"] = None
    return cfg


def action_changes(action: dict, settings: dict | None = None) -> dict:
    """Translate a schedule action into a controller change request.
    The heater's own off-timer is only touched when the action has a duration ("for N h")."""
    if not action["power"]:
        return {"power": False}
    ch: dict = {"power": True}
    if action.get("setpoint") is not None:
        ch["setpoint"] = action["setpoint"]
    ch["eco"] = bool(action.get("eco"))
    hours = int(action.get("hours") or 0)
    if hours:
        ch["timer_hours"] = min(hours, p.TIMER_MAX)
    return ch


def describe(action: dict) -> str:
    if not action["power"]:
        return "Turn off"
    if action.get("eco"):
        s = "Power-save"
    else:
        sp = action.get("setpoint")
        s = f"Heat to {sp}°" if sp is not None else "Turn on"
    if action.get("hours"):
        s += f" for {action['hours']} h"
    return s


def within(cfg: dict, now: dt.datetime, hours: float) -> list[dict]:
    """Occurrences that would actually run between now and now+hours (for the manual-timer check)."""
    end = now + dt.timedelta(hours=hours)
    return [u for u in upcoming(cfg, now, days=int(hours // 24) + 2, limit=200)
            if dt.datetime.strptime(u["at"], FMT) <= end and not u["skipped"] and not u["paused"]]


def on_timer_check(cfg: dict, now: dt.datetime, hours: int, days: int = 7) -> dict:
    """What the schedule does around an on-timer armed now (heater off, starts at now+hours).

    The heater's on-timer is cancelled by any power-on before it fires, and once it fires the heater
    runs with no off-timer (verified 2026-11-10). So report:
      cancels : events before the start that turn the heater on (each would cancel the on-timer)
      off     : the first event after the start that ends the heating, {at, name, action, how}
                (how = "off" for a turn-off, "timer" for an "on for N h" event, at = when it ends,
                runs_h = hours of heating from the start until then); None if nothing in `days` days does."""
    start = now + dt.timedelta(hours=hours)
    runs = [u for u in upcoming(cfg, now, days=days + int(hours // 24) + 1, limit=500)
            if not u["skipped"] and not u["paused"]]
    cancels, off = [], None
    for u in runs:
        at = dt.datetime.strptime(u["at"], FMT)
        a = u["action"]
        if at <= start:
            if a["power"]:
                cancels.append({"key": u["key"], "at": u["at"], "name": u["name"], "action": describe(a)})
            continue
        if not a["power"]:
            off = {"at": u["at"], "name": u["name"], "action": describe(a), "how": "off"}
        elif a.get("hours"):
            end = at + dt.timedelta(hours=int(a["hours"]))
            off = {"at": end.strftime(FMT), "name": u["name"], "action": describe(a), "how": "timer"}
        if off:
            off["runs_h"] = round((dt.datetime.strptime(off["at"], FMT) - start).total_seconds() / 3600, 1)
            break
    return {"start": start.strftime(FMT), "cancels": cancels, "off": off}


# ---------------- conflicts ----------------
def conflicts(cfg: dict, now: dt.datetime, days: int = 7) -> list[dict]:
    """Find problems in the coming `days`. Each: {level: error|warn|info, ids: [...], text}.
    Deduplicated per (kind, rule/event pair) so a daily clash is reported once."""
    occ = occurrences(cfg, now, now + dt.timedelta(days=days))
    max_on = int(cfg["settings"].get("max_on_hours") or 0)
    out: dict = {}

    def add(kind, level, ids, text):
        key = (kind,) + tuple(ids)
        out.setdefault(key, {"level": level, "ids": list(dict.fromkeys(ids)), "text": text})

    def label(o):
        return f"“{o['name']}”" if o["name"] else f"{describe(o['action'])} at {o['at'].strftime('%H:%M')}"

    for i, a in enumerate(occ):
        # same minute
        for b in occ[i + 1:]:
            if b["at"] != a["at"]:
                break
            add("same", "error", [a["id"], b["id"]],
                f"{label(a)} and {label(b)} both run at {a['at'].strftime('%a %H:%M')} — only one can run.")
        hours = int(a["action"].get("hours") or 0)
        if a["action"]["power"] and hours and max_on and hours > max_on:
            add("max", "warn", [a["id"]],
                f"{label(a)} runs for {hours} h but the safety check turns the heater off after {max_on} h.")
        if not (a["action"]["power"] and hours):
            continue
        end = a["at"] + dt.timedelta(hours=hours)
        for b in occ[i + 1:]:
            if b["at"] >= end:
                break
            if b["at"] == a["at"]:
                continue
            if not b["action"]["power"]:
                add("early", "info", [a["id"], b["id"]],
                    f"{label(b)} turns the heater off before {label(a)}'s {hours} h are over.")
            elif int(b["action"].get("hours") or 0):
                add("replace", "info", [a["id"], b["id"]],
                    f"{label(b)} starts during {label(a)}'s {hours} h and resets the heater's timer to "
                    f"{b['action']['hours']} h.")
            else:
                add("cutoff", "warn", [a["id"], b["id"]],
                    f"{label(a)}'s {hours} h timer is still running when {label(b)} starts — the heater will "
                    f"switch itself off at {end.strftime('%H:%M')}. Give {label(b)} its own duration, or shorten "
                    f"{label(a)}.")
    order = {"error": 0, "warn": 1, "info": 2}
    return sorted(out.values(), key=lambda c: order[c["level"]])


class Runner:
    """Stateful wrapper: call tick(now, heater_available) periodically; returns actions to apply.

    `last` is the last time the schedule was evaluated (persist it across restarts).
    `pending` is the latest missed occurrence waiting for the heater to come back.
    """

    def __init__(self, last: dt.datetime | None = None):
        self.last = last
        self.pending: dict | None = None
        self.notes: list[dict] = []   # occurrences that did NOT fire in the last tick, with an "outcome"

    def tick(self, cfg: dict, now: dt.datetime, available: bool) -> list[dict]:
        self.notes = []
        catchup = dt.timedelta(minutes=cfg["settings"]["catchup_minutes"])
        if self.last is None or self.last > now:      # first run or clock went backwards
            self.last = now - catchup
        start = max(self.last, now - max(catchup, dt.timedelta(minutes=2)))
        skips = set(cfg["settings"].get("skips", []))
        cands = []
        for o in occurrences(cfg, start, now):
            if o["key"] in skips:
                self.notes.append({**o, "outcome": "skipped"})
            elif paused(cfg, o["at"]):
                self.notes.append({**o, "outcome": "paused"})
            else:
                cands.append(o)
        self.last = now
        if cands:
            for o in cands[:-1]:   # only the latest missed occurrence is caught up
                self.notes.append({**o, "outcome": "superseded"})
            latest = cands[-1]
            if self.pending is not None and self.pending["key"] != latest["key"]:
                self.notes.append({**self.pending, "outcome": "superseded"})
            if available:
                self.pending = None
                return [latest]
            self.pending = latest
            return []
        if self.pending is not None:
            if now - self.pending["at"] > catchup:
                self.notes.append({**self.pending, "outcome": "missed"})
                self.pending = None                   # too old, give up
            elif available:
                o, self.pending = self.pending, None
                return [o]
        return []
