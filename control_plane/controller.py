"""Heater controller: turns desired changes into protocol frames and verifies the result.

Pure logic (no I/O) so it is unit-testable; heater.py and server.py wire it to the pipe, HTTP and MQTT.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import protocol as p

FIELDS = ("power", "setpoint", "eco", "timer_hours", "voice")


# The heater commits a new setpoint ~3 s after the command (display flashes first). A power-save
# command sent inside that window is reported as applied, but the delayed setpoint commit still
# takes over the heating (observed 2026-11-09). So anything after a setpoint waits for the commit.
SETPOINT_COMMIT_S = 3.5
GAP_S = 0.4


@dataclass
class Plan:
    frames: list[bytes] = field(default_factory=list)
    expect: dict = field(default_factory=dict)   # field -> expected value after the frames apply
    delays: list[float] = field(default_factory=list)  # pause after each frame (same length as frames)


def normalize(changes: dict) -> dict:
    """Validate/coerce a user change request. Raises ValueError on bad input."""
    out: dict = {}
    for k, v in changes.items():
        if k == "power" or k == "eco" or k == "voice":
            if isinstance(v, str):
                v = v.strip().lower() in ("1", "on", "true", "heat", "yes")
            out[k] = bool(v)
        elif k == "setpoint":
            t = int(round(float(v)))
            if not p.TEMP_MIN <= t <= p.TEMP_MAX:
                raise ValueError(f"setpoint must be {p.TEMP_MIN}..{p.TEMP_MAX}")
            out[k] = t
        elif k in ("timer_hours", "timer"):
            h = int(round(float(v)))
            if not 0 <= h <= p.TIMER_MAX:
                raise ValueError(f"timer must be 0..{p.TIMER_MAX}")
            out["timer_hours"] = h
        else:
            raise ValueError(f"unknown field {k}")
    return out


def plan(current: p.Status | None, changes: dict) -> Plan:
    """Build the frame sequence for `changes` against `current` state, skipping no-ops.

    Ordering rules learned from the device:
      * power on first, power off last;
      * setting the setpoint cancels power-save, so eco is (re)applied after a setpoint change.
    """
    ch = normalize(changes)
    cur = {
        "power": current.power if current else None,
        "setpoint": current.setpoint if current else None,
        "eco": current.eco if current else None,
        "timer_hours": current.timer_hours if current else None,
        "voice": current.voice if current else None,
    }
    pl = Plan()

    # While the heater is off it ignores setpoint and power-save (verified), so refuse them unless this
    # request powers on. The timer is accepted while off: it is then an on-timer (delayed start).
    on_during = cur["power"] is True or ch.get("power") is True
    if not on_during:
        blocked = [k for k in ("setpoint", "eco") if k in ch and ch[k] != cur[k]]
        if blocked:
            raise ValueError(f"heater is off: turn it on to change {', '.join(blocked)}")

    def add(frame: bytes, key: str, val) -> None:
        if pl.frames:   # the previous frame needs its pause before this one
            prev_is_setpoint = pl.frames[-1][3] == p.CMD_SETPOINT
            pl.delays[-1] = SETPOINT_COMMIT_S if prev_is_setpoint else GAP_S
        pl.frames.append(frame)
        pl.delays.append(0.0)
        pl.expect[key] = val

    if ch.get("power") is True and cur["power"] is not True:
        add(p.power(True), "power", True)
    setpoint_sent = False
    if "setpoint" in ch and ch["setpoint"] != cur["setpoint"]:
        add(p.setpoint(ch["setpoint"]), "setpoint", ch["setpoint"])
        setpoint_sent = True
    eco_after = cur["eco"] if not setpoint_sent else False   # setpoint clears eco
    if "eco" in ch and ch["eco"] != eco_after:
        add(p.eco(ch["eco"]), "eco", ch["eco"])
    elif setpoint_sent and cur["eco"]:
        pl.expect["eco"] = False
    if "timer_hours" in ch and ch["timer_hours"] != cur["timer_hours"]:
        add(p.timer(ch["timer_hours"]), "timer_hours", ch["timer_hours"])
    if "voice" in ch and ch["voice"] != cur["voice"]:
        add(p.voice(ch["voice"]), "voice", ch["voice"])
    if ch.get("power") is False and cur["power"] is not False:
        add(p.power(False), "power", False)
    return pl


def summarize(status: p.Status | None) -> str:
    """Short human state, e.g. 'off', 'on · 23°C', 'power-save', '+ timer 2 h'."""
    if status is None:
        return "unknown"
    if not status.power:
        s = "off"
    elif status.eco:
        s = "power-save"
    else:
        sp = status.setpoint if p.TEMP_MIN <= status.setpoint <= p.TEMP_MAX else p.DEFAULT_SETPOINT
        s = f"on · {sp}°C"
    if status.timer_hours:
        s += f" · {'timer' if status.power else 'on in'} {status.timer_hours} h"
    return s


def preview(status: p.Status | None, changes: dict) -> dict:
    """What `changes` would do against `status` right now: {effect, text}.
    effect: change | noop | blocked | unknown"""
    if status is None:
        return {"effect": "unknown", "text": "Heater offline — applies when it's back"}
    try:
        pl = plan(status, changes)
    except ValueError as e:
        return {"effect": "blocked", "text": str(e)}
    if not pl.frames:
        return {"effect": "noop", "text": f"No change needed — already {summarize(status)}"}
    parts = []
    e = pl.expect
    if e.get("power") is False:
        parts.append("turn off")
    elif e.get("power") is True:
        parts.append("turn on")
    if "setpoint" in e:
        parts.append(f"heat to {e['setpoint']}°")
    if e.get("eco") is True:
        parts.append("power-save")
    elif e.get("eco") is False and status.eco and "setpoint" not in e:
        parts.append("end power-save")
    if "timer_hours" in e:
        kind = "off-timer" if e.get("power", status.power) else "on-timer"
        parts.append(f"arm {e['timer_hours']} h {kind}" if e["timer_hours"] else f"cancel {kind}")
    if "voice" in e:
        parts.append("voice " + ("on" if e["voice"] else "off"))
    txt = ", ".join(parts) or "update settings"
    return {"effect": "change", "text": f"Will {txt} (now {summarize(status)})"}


def satisfied(status: p.Status | None, expect: dict) -> bool:
    if status is None:
        return False
    for k, v in expect.items():
        if k == "timer_hours":
            # the timer counts down; a set timer may already read one hour less
            if v == 0 and status.timer_hours != 0:
                return False
            if v and not (v - 1 <= status.timer_hours <= v):
                return False
        elif getattr(status, k) != v:
            return False
    return True


def to_state(status: p.Status | None, ble: str, rejected: bool) -> dict:
    s: dict = {"available": status is not None and ble == "connected", "ble": ble, "rejected": rejected}
    if status is None:
        return s
    s["can"] = {   # which controls have an effect in the current state (UI greys out the rest)
        "power": True,
        "setpoint": status.power,
        "eco": status.power,
        "timer": True,             # while off it is an on-timer (the heater starts when it runs out)
        "voice": True,
    }
    if status is not None:
        s.update(
            power=status.power,
            mode="heat" if status.power else "off",
            # After a power cut the BLE module reports setpoint 0 until one is set, while the heater
            # itself runs at its boot default (23 C) -> show the default.
            setpoint=status.setpoint if p.TEMP_MIN <= status.setpoint <= p.TEMP_MAX else p.DEFAULT_SETPOINT,
            eco=status.eco,
            preset="eco" if status.eco else "none",
            timer_hours=status.timer_hours,
            voice=status.voice,
            level=status.level,
        )
    return s
