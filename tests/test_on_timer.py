import datetime as dt
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "control_plane"))
import protocol as p  # noqa: E402
import scheduler as sch  # noqa: E402
import server  # noqa: E402

NOW = dt.datetime(2026, 11, 9, 6, 0)   # Monday


def st(power, timer=0):
    return p.Status(power=power, timer_hours=timer, mode=0, level=0, setpoint=23, voice=False)


class FakeHeater:
    def __init__(self, status):
        self.status, self.sent, self.log = status, [], []

    def status_now(self):
        return self.status

    def submit(self, changes, source="app", on_done=None):
        self.sent.append(changes)

    def record(self, kind, text):
        self.log.append((kind, text))


def make_app(status, rules=()):
    a = server.App.__new__(server.App)          # no files, threads or pipe
    a.lock = threading.RLock()
    a.cfg = sch.validate({"rules": list(rules)})
    a.manual_timer = a.on_timer = None
    a.heater = FakeHeater(status)
    a.now = lambda: NOW
    a._save_state = a.save_cfg = lambda: None
    return a


WAKE = {"id": "wake", "name": "Wake", "days": [0], "time": "06:45", "action": {"power": True, "setpoint": 24}}
LEAVE = {"id": "leave", "name": "Leave", "days": [0], "time": "09:00", "action": {"power": False}}


def test_on_timer_asks_when_an_event_would_cancel_it():
    a = make_app(st(False), [WAKE, LEAVE])
    with pytest.raises(server.OnTimerCheck) as e:
        a.set_manual_timer({"timer_hours": 1}, None)
    assert [c["name"] for c in e.value.info["cancels"]] == ["Wake"] and not e.value.info["long"]
    assert a.heater.sent == []
    a.set_manual_timer({"timer_hours": 1}, "timer")              # timer first: skip Wake
    assert a.heater.sent == [{"timer_hours": 1}]
    assert a.cfg["settings"]["skips"] == ["r:wake:2026-11-09T06:45"]
    assert a.on_timer["start"] == "2026-11-09T07:00"


def test_on_timer_asks_when_nothing_turns_it_off_and_sets_off_timer_when_it_fires():
    a = make_app(st(False))
    with pytest.raises(server.OnTimerCheck) as e:
        a.set_manual_timer({"timer_hours": 1}, None)
    assert e.value.info["off"] is None and e.value.info["long"]
    a.set_manual_timer({"timer_hours": 1}, "ok", off_after=2)
    a.on_timer["set_at"] -= 60
    a.heater.status = st(False, 1)
    a._track_on_timer(a.heater.status, NOW + dt.timedelta(minutes=30))   # pending
    assert a.on_timer and a.heater.sent == [{"timer_hours": 1}]
    a.heater.status = st(True, 0)                                       # fired at 07:00
    a._track_on_timer(a.heater.status, NOW + dt.timedelta(hours=1, seconds=10))
    assert a.on_timer is None
    assert a.heater.sent[-1] == {"timer_hours": 2}


def test_on_timer_without_questions_when_schedule_ends_it_soon():
    a = make_app(st(False), [LEAVE])
    a.set_manual_timer({"timer_hours": 1}, None)                 # 07:00 start, Leave 09:00 -> no prompt
    assert a.heater.sent == [{"timer_hours": 1}]


def test_manual_power_on_before_start_cancels_on_timer():
    a = make_app(st(False), [LEAVE])
    a.set_manual_timer({"timer_hours": 2}, None)
    a.on_timer["set_at"] -= 60
    a._track_on_timer(st(True, 0), NOW + dt.timedelta(minutes=20))
    assert a.on_timer is None
    assert a.heater.log[-1][1].startswith("Heater was turned on before its on-timer")
    assert a.heater.sent == [{"timer_hours": 2}]                   # no follow-up off-timer
