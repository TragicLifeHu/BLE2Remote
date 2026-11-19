import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "control_plane"))
import heater as hm  # noqa: E402


class FakeTimer:
    made: list = []

    def __init__(self, delay, fn, args=()):
        self.fn, self.args, self.daemon = fn, args, False
        FakeTimer.made.append(self)

    def start(self):
        pass

    def fire(self):
        self.fn(*self.args)


def make(monkeypatch, clock):
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)   # no pipe / worker threads
    monkeypatch.setattr(hm.threading, "Timer", FakeTimer)
    monkeypatch.setattr(hm.time, "time", lambda: clock[0])
    FakeTimer.made = []
    h = hm.Heater("127.0.0.1", 1)
    h._send = lambda line: True
    return h


def texts(h):
    return [a["text"] for a in reversed(h.activity)]


def test_short_link_blips_stay_out_of_activity(monkeypatch):
    clock = [1000.0]
    h = make(monkeypatch, clock)
    h._on_line("STATE connected")
    assert texts(h) == ["Heater connected"]
    since = h.connected_since
    for _ in range(5):                       # heater drops idle link, iOS reconnects within a second
        clock[0] += 60
        h._on_line("STATE connecting")
        clock[0] += 1
        h._on_line("STATE connected")
    for t in FakeTimer.made:                 # grace timers fire late: link is back, nothing to log
        t.fire()
    assert texts(h) == ["Heater connected"]
    assert h.connected_since == since


def test_long_outage_logged_once_with_duration(monkeypatch):
    clock = [1000.0]
    h = make(monkeypatch, clock)
    h._on_line("STATE connected")
    clock[0] += 10
    h._on_line("STATE connecting")
    clock[0] += hm.LINK_GRACE_S
    FakeTimer.made[-1].fire()
    assert texts(h)[-1] == "Heater link lost (connecting)"
    assert h.connected_since is None
    clock[0] += 600 - hm.LINK_GRACE_S
    h._on_line("STATE connected")
    assert texts(h)[-1] == "Heater reconnected after 10 min"
    assert h.connected_since == clock[0]
