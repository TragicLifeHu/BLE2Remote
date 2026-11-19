import datetime as dt
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "control_plane"))
import scheduler as s  # noqa: E402

D = dt.datetime
# 2026-11-09 is a Monday
MON_0630 = D(2026, 11, 9, 6, 30)


def cfg(**settings):
    c = s.validate({
        "rules": [
            {"id": "wake", "name": "Wake", "days": [0, 1, 2, 3, 4], "time": "06:45",
             "action": {"power": True, "setpoint": 24}},
            {"id": "leave", "name": "Leave", "days": [0, 1, 2, 3, 4], "time": "08:15", "action": {"power": False}},
            {"id": "wkend", "name": "Weekend", "days": [5, 6], "time": "08:00",
             "action": {"power": True, "eco": True}},
        ],
        "events": [{"id": "trip", "name": "Back home", "at": "2026-11-12T18:00",
                    "action": {"power": True, "setpoint": 22}}],
        "settings": settings,
    })
    return c


def test_validate_rejects_bad_input():
    with pytest.raises(ValueError):
        s.validate({"rules": [{"days": [], "time": "07:00", "action": {"power": False}}]})
    with pytest.raises(ValueError):
        s.validate({"rules": [{"days": [1], "time": "25:00", "action": {"power": False}}]})
    with pytest.raises(ValueError):
        s.validate({"rules": [{"days": [1], "time": "07:00", "action": {"power": True, "setpoint": 50}}]})


def test_occurrences_week():
    occ = s.occurrences(cfg(), MON_0630, MON_0630 + dt.timedelta(days=7))
    names = [(o["at"].strftime("%a %H:%M"), o["name"]) for o in occ]
    assert names[:3] == [("Mon 06:45", "Wake"), ("Mon 08:15", "Leave"), ("Tue 06:45", "Wake")]
    assert ("Thu 18:00", "Back home") in names
    assert ("Sat 08:00", "Weekend") in names and ("Sun 08:00", "Weekend") in names
    assert len(occ) == 5 * 2 + 2 + 1


def test_runner_fires_once_at_time():
    c, r = cfg(), s.Runner(last=MON_0630)
    assert r.tick(c, D(2026, 11, 9, 6, 44), True) == []
    out = r.tick(c, D(2026, 11, 9, 6, 45, 10), True)
    assert [o["name"] for o in out] == ["Wake"]
    assert r.tick(c, D(2026, 11, 9, 6, 45, 25), True) == []          # not again


def test_skip_and_pause():
    c = cfg(skips=["r:wake:2026-11-09T06:45"])
    r = s.Runner(last=MON_0630)
    assert r.tick(c, D(2026, 11, 9, 6, 46), True) == []
    c = cfg(pause_until="2026-11-11T00:00")
    r = s.Runner(last=MON_0630)
    assert r.tick(c, D(2026, 11, 9, 6, 46), True) == []
    r = s.Runner(last=D(2026, 11, 11, 6, 40))
    assert [o["name"] for o in r.tick(c, D(2026, 11, 11, 6, 46), True)] == ["Wake"]


def test_catch_up_when_heater_returns():
    c, r = cfg(catchup_minutes=30), s.Runner(last=MON_0630)
    assert r.tick(c, D(2026, 11, 9, 6, 46), False) == []             # heater offline at 06:45
    assert r.pending["name"] == "Wake"
    out = r.tick(c, D(2026, 11, 9, 7, 5), True)                       # back within 30 min
    assert [o["name"] for o in out] == ["Wake"]
    r = s.Runner(last=MON_0630)
    r.tick(c, D(2026, 11, 9, 6, 46), False)
    assert r.tick(c, D(2026, 11, 9, 7, 30), True) == []               # too late, dropped


def test_restart_catches_up_only_latest_in_window():
    c = cfg(catchup_minutes=30)
    r = s.Runner(last=None)                                           # fresh start at 08:20
    out = r.tick(c, D(2026, 11, 9, 8, 20), True)
    assert [o["name"] for o in out] == ["Leave"]                      # 06:45 is outside the window


def test_upcoming_marks_skips_and_prune():
    c = cfg(skips=["r:wake:2026-11-10T06:45", "r:wake:2026-11-01T06:45"])
    up = s.upcoming(c, MON_0630, days=2)
    tue = [u for u in up if u["key"] == "r:wake:2026-11-10T06:45"][0]
    assert tue["skipped"] is True
    s.prune(c, MON_0630)
    assert c["settings"]["skips"] == ["r:wake:2026-11-10T06:45"]


def test_action_changes_timer_only_when_asked():
    st = {"max_on_hours": 6}
    assert s.action_changes({"power": True, "setpoint": 24, "eco": False}, st) == \
        {"power": True, "setpoint": 24, "eco": False}
    assert s.action_changes({"power": True, "setpoint": 24, "eco": False, "hours": 2}, st)["timer_hours"] == 2
    assert s.action_changes({"power": False, "setpoint": None, "eco": False}, st) == {"power": False}
    assert s.describe({"power": True, "setpoint": None, "eco": True}) == "Power-save"


def test_runner_notes_skipped_missed_superseded():
    c = cfg(skips=["r:wake:2026-11-09T06:45"], catchup_minutes=30)
    r = s.Runner(last=MON_0630)
    assert r.tick(c, D(2026, 11, 9, 6, 46), True) == []
    assert [(n["name"], n["outcome"]) for n in r.notes] == [("Wake", "skipped")]
    c = cfg(catchup_minutes=30)
    r = s.Runner(last=MON_0630)
    r.tick(c, D(2026, 11, 9, 6, 46), False)
    r.tick(c, D(2026, 11, 9, 7, 30), False)
    assert [(n["name"], n["outcome"]) for n in r.notes] == [("Wake", "missed")]
    # restart after both 06:45 and 08:15 passed within a long catch-up window
    c = cfg(catchup_minutes=120)
    r = s.Runner(last=None)
    out = r.tick(c, D(2026, 11, 9, 8, 20), True)
    assert [o["name"] for o in out] == ["Leave"]
    assert [(n["name"], n["outcome"]) for n in r.notes] == [("Wake", "superseded")]


def test_describe_with_duration():
    assert s.describe({"power": True, "setpoint": 23, "eco": False, "hours": 2}) == "Heat to 23° for 2 h"
    assert s.describe({"power": True, "setpoint": None, "eco": True, "hours": 0}) == "Power-save"


def test_conflicts():
    c = s.validate({"rules": [
        {"id": "wake", "name": "Wake", "days": [0], "time": "07:00", "action": {"power": True, "setpoint": 23, "hours": 2}},
        {"id": "eco", "name": "Eco", "days": [0], "time": "08:00", "action": {"power": True, "eco": True}},
        {"id": "off", "name": "Off", "days": [0], "time": "08:30", "action": {"power": False}},
        {"id": "long", "name": "Long", "days": [1], "time": "18:00", "action": {"power": True, "hours": 12}},
    ], "settings": {"max_on_hours": 10}})
    found = {(x["level"], tuple(x["ids"])) for x in s.conflicts(c, MON_0630)}
    assert ("warn", ("wake", "eco")) in found        # wake's timer still cuts off eco at 09:00
    assert ("warn", ("long",)) in found              # 12 h > 10 h safety limit
    assert not any(lvl == "error" for lvl, _ in found)
    c["rules"].append({"id": "dup", "name": "Dup", "enabled": True, "days": [0], "time": "07:00",
                       "action": {"power": False, "setpoint": None, "eco": False, "hours": 0}})
    errs = [x for x in s.conflicts(c, MON_0630) if x["level"] == "error"]
    assert errs and set(errs[0]["ids"]) == {"wake", "dup"}
    # wake -> off without the eco rule in between is just "ends early"
    c2 = s.validate({"rules": [r for r in c["rules"] if r["id"] in ("wake", "off")]})
    c2["rules"][1]["time"] = "08:30"
    assert [x["level"] for x in s.conflicts(c2, MON_0630)] == ["info"]


def test_within_window_for_manual_timer():
    c = cfg()
    got = [(o["name"], o["at"]) for o in s.within(c, D(2026, 11, 9, 6, 0), 3)]
    assert got == [("Wake", "2026-11-09T06:45"), ("Leave", "2026-11-09T08:15")]
    c["settings"]["skips"] = ["r:wake:2026-11-09T06:45"]
    assert [o["name"] for o in s.within(c, D(2026, 11, 9, 6, 0), 3)] == ["Leave"]
    assert s.within(c, D(2026, 11, 9, 9, 0), 2) == []


def test_on_timer_check():
    c = cfg()
    # Mon 06:00 + 1 h: starts 07:00, Wake at 06:45 would turn it on first (cancels), Leave ends it 08:15
    info = s.on_timer_check(c, D(2026, 11, 9, 6, 0), 1)
    assert info["start"] == "2026-11-09T07:00"
    assert [x["name"] for x in info["cancels"]] == ["Wake"]
    assert info["off"] == {"at": "2026-11-09T08:15", "name": "Leave", "action": "Turn off", "how": "off", "runs_h": 1.2}
    # a skipped event doesn't cancel it
    c["settings"]["skips"] = ["r:wake:2026-11-09T06:45"]
    assert s.on_timer_check(c, D(2026, 11, 9, 6, 0), 1)["cancels"] == []
    # Fri 20:00 + 2 h: weekend power-save events (on, no duration) keep it running until Monday's Leave
    info = s.on_timer_check(cfg(), D(2026, 11, 13, 20, 0), 2)
    assert info["cancels"] == [] and info["off"]["at"] == "2026-11-16T08:15" and info["off"]["runs_h"] == 58.2
    # ... and nothing at all within a shorter horizon
    assert s.on_timer_check(cfg(), D(2026, 11, 13, 20, 0), 2, days=1)["off"] is None
    # an "on for N h" event after the start ends the heating with its own timer
    c = s.validate({"rules": [{"id": "w", "name": "Warm", "days": [0], "time": "09:00",
                               "action": {"power": True, "setpoint": 22, "hours": 2}}]})
    off = s.on_timer_check(c, D(2026, 11, 9, 6, 0), 1)["off"]
    assert off == {"at": "2026-11-09T11:00", "name": "Warm", "action": "Heat to 22° for 2 h", "how": "timer",
                   "runs_h": 4.0}
