import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "control_plane"))
import protocol as p  # noqa: E402

# Frames captured from the platform (re/captures/ble-session1.log): (hex, builder)
CAPTURED = [
    ("aa030200000555", lambda: p.query(seq=0x02)),
    ("aa030501010a55", lambda: p.power(True, seq=0x05)),
    ("aa030601000a55", lambda: p.power(False, seq=0x06)),
    ("aa030a06122555", lambda: p.setpoint(18, seq=0x0A)),
    ("aa030b06132755", lambda: p.setpoint(19, seq=0x0B)),
    ("aa030c06142955", lambda: p.setpoint(20, seq=0x0C)),
    ("aa030d06233955", lambda: p.setpoint(35, seq=0x0D)),
    ("aa030e06172e55", lambda: p.setpoint(23, seq=0x0E)),
    ("aa030f09011c55", lambda: p.eco(True, seq=0x0F)),
    ("aa031009001c55", lambda: p.eco(False, seq=0x10)),
    ("aa031105011a55", lambda: p.timer(1, seq=0x11)),
    ("aa031205021c55", lambda: p.timer(2, seq=0x12)),
    ("aa031305183355", lambda: p.timer(24, seq=0x13)),
    ("aa031405001c55", lambda: p.timer(0, seq=0x14)),
    ("aa031501001955", lambda: p.power(False, seq=0x15)),
    ("aa03060f011955", lambda: p.voice(True, seq=0x06)),
    ("aa03070f001955", lambda: p.voice(False, seq=0x07)),
    ("aa030aff000c55", lambda: p.keepalive(seq=0x0A)),
    ("aa0303fe000455", lambda: p.build(0xFE, 0, seq=0x03)),
    ("aa0304f800ff55", lambda: p.build(0xF8, 0, seq=0x04)),
]


@pytest.mark.parametrize("hexframe,builder", CAPTURED)
def test_builders_match_capture(hexframe, builder):
    assert builder().hex() == hexframe


# Status frames observed from the PC test (tools/ble_send.py)
@pytest.mark.parametrize("hexframe,expect", [
    ("aa0800530000000017007255", dict(power=False, timer_hours=0, mode=0, level=0, setpoint=23)),
    ("aa0800530100000017007355", dict(power=True, timer_hours=0, mode=0, level=0, setpoint=23)),
    ("aa0800530100030217007855", dict(power=True, timer_hours=0, mode=3, level=2, setpoint=23)),
    ("aa0800530102030217007a55", dict(power=True, timer_hours=2, mode=3, level=2, setpoint=23)),
    ("aa0800530102000014007255", dict(power=True, timer_hours=2, mode=0, level=0, setpoint=20)),
    ("aa0800530100000017017455", dict(power=True, mode=0, setpoint=23, voice=True)),
    ("aa0800530100000017007355", dict(power=True, setpoint=23, voice=False)),
])
def test_parse_status(hexframe, expect):
    s = p.parse_status(bytes.fromhex(hexframe))
    for k, v in expect.items():
        assert getattr(s, k) == v


def test_bad_checksum_rejected():
    with pytest.raises(ValueError):
        p.parse_status(bytes.fromhex("aa0800530000000017007355"))


def test_ranges():
    with pytest.raises(ValueError):
        p.setpoint(17)
    with pytest.raises(ValueError):
        p.timer(25)
