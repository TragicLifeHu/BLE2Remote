"""Heater's BLE protocol (AirMeter UAF028, service FFB0). Pure, dependency-free.

Frame:  AA | LEN | payload[LEN] | CHK | 55     CHK = (LEN + sum(payload)) & 0xFF
Commands (write to FFB1, write-without-response), LEN=3, payload = SEQ CMD VAL.
Status  (notify on FFB2), LEN=8, payload = 00 53 POWER TIMER MODE LEVEL SETPOINT VOICE.
Keep-alive: app sends CMD 0xFF every 20 s while idle (no reply).
See docs/protocol.md.
"""
from __future__ import annotations

from dataclasses import dataclass

SERVICE_UUID = "0000ffb0-0000-1000-8000-00805f9b34fb"
TX_UUID = "0000ffb1-0000-1000-8000-00805f9b34fb"
RX_UUID = "0000ffb2-0000-1000-8000-00805f9b34fb"

CMD_QUERY = 0x00
CMD_POWER = 0x01
CMD_TIMER = 0x05
CMD_SETPOINT = 0x06
CMD_ECO = 0x09
CMD_VOICE = 0x0F
CMD_KEEPALIVE = 0xFF

STATUS_TYPE = 0x53
TEMP_MIN, TEMP_MAX = 18, 35
DEFAULT_SETPOINT = 23   # heater's setpoint after power-up (BLE status reports 0 until one is set)
TIMER_MAX = 24


def _wrap(payload: bytes) -> bytes:
    n = len(payload)
    return bytes([0xAA, n]) + payload + bytes([(n + sum(payload)) & 0xFF, 0x55])


def build(cmd: int, val: int = 0, seq: int = 0) -> bytes:
    return _wrap(bytes([seq & 0xFF, cmd & 0xFF, val & 0xFF]))


def query(seq: int = 0) -> bytes:
    return build(CMD_QUERY, 0, seq)


def power(on: bool, seq: int = 0) -> bytes:
    return build(CMD_POWER, 1 if on else 0, seq)


def eco(on: bool, seq: int = 0) -> bytes:
    return build(CMD_ECO, 1 if on else 0, seq)


def voice(on: bool, seq: int = 0) -> bytes:
    return build(CMD_VOICE, 1 if on else 0, seq)


def keepalive(seq: int = 0) -> bytes:
    return build(CMD_KEEPALIVE, 0, seq)


def setpoint(celsius: int, seq: int = 0) -> bytes:
    if not TEMP_MIN <= celsius <= TEMP_MAX:
        raise ValueError(f"setpoint must be {TEMP_MIN}..{TEMP_MAX}")
    return build(CMD_SETPOINT, celsius, seq)


def timer(hours: int, seq: int = 0) -> bytes:
    """Off-timer in hours; 0 cancels."""
    if not 0 <= hours <= TIMER_MAX:
        raise ValueError(f"timer must be 0..{TIMER_MAX}")
    return build(CMD_TIMER, hours, seq)


@dataclass(frozen=True)
class Status:
    power: bool
    timer_hours: int
    mode: int       # 0 = normal, 3 = power-save (observed)
    level: int      # 2 while power-save (observed); meaning TBD
    setpoint: int
    voice: bool     # on-device voice control enabled

    @property
    def eco(self) -> bool:
        return self.mode != 0


def unwrap(frame: bytes) -> bytes:
    if len(frame) < 4 or frame[0] != 0xAA or frame[-1] != 0x55:
        raise ValueError(f"bad framing: {frame.hex()}")
    n = frame[1]
    if len(frame) != n + 4:
        raise ValueError(f"bad length: {frame.hex()}")
    payload = frame[2:2 + n]
    if (n + sum(payload)) & 0xFF != frame[2 + n]:
        raise ValueError(f"bad checksum: {frame.hex()}")
    return payload


def parse_status(frame: bytes) -> Status:
    p = unwrap(frame)
    if len(p) != 8 or p[1] != STATUS_TYPE:
        raise ValueError(f"not a status frame: {frame.hex()}")
    return Status(power=bool(p[2]), timer_hours=p[3], mode=p[4], level=p[5],
                  setpoint=p[6], voice=bool(p[7]))
