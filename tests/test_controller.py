import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "control_plane"))
import controller as ctl  # noqa: E402
import protocol as p  # noqa: E402


def st(power=True, timer=0, eco=False, sp=23, voice=False):
    return p.Status(power=power, timer_hours=timer, mode=3 if eco else 0, level=2 if eco else 0,
                    setpoint=sp, voice=voice)


def cmds(plan):
    return [(f[3], f[4]) for f in plan.frames]


def test_noop_changes_send_nothing():
    assert ctl.plan(st(), {"power": True, "setpoint": 23, "eco": False}).frames == []


def test_power_on_first_power_off_last():
    pl = ctl.plan(st(power=False), {"setpoint": 25, "power": True})
    assert cmds(pl) == [(p.CMD_POWER, 1), (p.CMD_SETPOINT, 25)]
    pl = ctl.plan(st(), {"power": False, "timer_hours": 2})
    assert cmds(pl) == [(p.CMD_TIMER, 2), (p.CMD_POWER, 0)]


def test_setpoint_clears_eco_and_eco_is_reapplied():
    pl = ctl.plan(st(eco=True), {"setpoint": 20, "eco": True})
    assert cmds(pl) == [(p.CMD_SETPOINT, 20), (p.CMD_ECO, 1)]
    pl = ctl.plan(st(eco=True), {"setpoint": 20})
    assert cmds(pl) == [(p.CMD_SETPOINT, 20)]
    assert pl.expect["eco"] is False


def test_validation():
    with pytest.raises(ValueError):
        ctl.plan(st(), {"setpoint": 40})
    with pytest.raises(ValueError):
        ctl.plan(st(), {"bogus": 1})
    assert ctl.normalize({"power": "heat", "timer": "3", "setpoint": "21.6"}) == \
        {"power": True, "timer_hours": 3, "setpoint": 22}


def test_satisfied_tolerates_timer_countdown():
    assert ctl.satisfied(st(timer=1), {"timer_hours": 2})
    assert not ctl.satisfied(st(timer=0), {"timer_hours": 2})
    assert not ctl.satisfied(st(sp=23), {"setpoint": 24})   # child lock case


def test_to_state():
    s = ctl.to_state(st(eco=True), "connected", False)
    assert s["available"] and s["mode"] == "heat" and s["preset"] == "eco"
    assert ctl.to_state(None, "scanning", False)["available"] is False


def test_after_power_cut_setpoint_is_device_default():
    cold = p.parse_status(bytes.fromhex("aa0800530000000000005b55"))  # observed after unplug/replug
    assert cold.setpoint == 0 and not cold.power
    assert ctl.to_state(cold, "connected", False)["setpoint"] == p.DEFAULT_SETPOINT
    # once powered on (still setpoint 0), requesting 23 sends the frame so the module learns it
    on = p.parse_status(bytes.fromhex("aa0800530100000000005c55"))
    assert [f.hex() for f in ctl.plan(on, {"setpoint": 23}).frames] == [p.setpoint(23).hex()]


def test_off_state_blocks_setpoint_eco_but_not_timer_or_voice():
    with pytest.raises(ValueError):
        ctl.plan(st(power=False), {"setpoint": 25})
    with pytest.raises(ValueError):
        ctl.plan(st(power=False), {"eco": True})
    # while off, the timer is an on-timer (accepted by the heater, re-captures/off-state-test.log)
    assert cmds(ctl.plan(st(power=False), {"timer_hours": 2})) == [(p.CMD_TIMER, 2)]
    assert ctl.preview(st(power=False), {"timer_hours": 2})["text"].startswith("Will arm 2 h on-timer")
    assert ctl.summarize(st(power=False, timer=2)) == "off · on in 2 h"
    assert cmds(ctl.plan(st(power=False), {"voice": True})) == [(p.CMD_VOICE, 1)]
    # allowed when the same request powers on (power goes first)
    assert cmds(ctl.plan(st(power=False), {"power": True, "eco": True})) == [(p.CMD_POWER, 1), (p.CMD_ECO, 1)]
    # allowed before powering off
    assert cmds(ctl.plan(st(), {"timer_hours": 0, "power": False})) == [(p.CMD_POWER, 0)]
    s = ctl.to_state(st(power=False), "connected", False)
    assert s["can"]["setpoint"] is False and s["can"]["voice"] is True and s["can"]["timer"] is True


def test_commands_after_setpoint_wait_for_commit():
    pl = ctl.plan(st(), {"setpoint": 24, "eco": True})
    assert cmds(pl) == [(p.CMD_SETPOINT, 24), (p.CMD_ECO, 1)]
    assert pl.delays == [ctl.SETPOINT_COMMIT_S, 0.0]
    pl = ctl.plan(st(power=False), {"power": True, "setpoint": 25, "timer_hours": 2})
    assert pl.delays == [ctl.GAP_S, ctl.SETPOINT_COMMIT_S, 0.0]
    assert ctl.plan(st(), {"setpoint": 20}).delays == [0.0]


def test_preview():
    assert ctl.preview(st(power=False), {"power": False})["effect"] == "noop"
    assert "already off" in ctl.preview(st(power=False), {"power": False})["text"]
    pv = ctl.preview(st(sp=24), {"power": True, "setpoint": 22, "eco": False})
    assert pv["effect"] == "change" and "heat to 22°" in pv["text"] and "now on · 24°C" in pv["text"]
    assert ctl.preview(None, {"power": False})["effect"] == "unknown"
    assert ctl.preview(st(power=False), {"setpoint": 22})["effect"] == "blocked"
    assert ctl.summarize(st(eco=True, timer=2)) == "power-save · timer 2 h"
