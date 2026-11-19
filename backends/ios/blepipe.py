"""blepipe — BLE <-> TCP line pipe for the heater (jailbroken iPhone, CoreBluetooth).

Holds one BLE connection to the paired peripheral, auto-reconnects, sends a keep-alive frame when
idle, and relays bytes to/from TCP clients. Knows nothing about the heater protocol beyond UUIDs.

TCP line protocol (UTF-8, '\\n' terminated):
  client -> pipe : WRITE <hex> | SCAN [seconds] | PAIR <ios-uuid> | FORGET | RELEASE <seconds>
                   QUERYSTATE | PING | AUTH <token>
  pipe -> client : HELLO blepipe <ver> | STATE <state> | PAIRED <ios-uuid|-> | NOTIFY <char> <hex>
                   INFO <ios-uuid> <name> | RSSI <dBm>  (while connected, RSSI every 30 s)
                   DEVICE <ios-uuid> <rssi> <mfr-hex|-> <name> | SCANDONE | OK | ERR <reason> | PONG
  states: unpaired | searching | connecting | connected | released

The iOS peripheral identifier (stable per phone) is stored in --id-file. Reconnects use
retrievePeripheralsWithIdentifiers + a pending connect, which works while the screen is locked
(scans are heavily throttled then).

Needs the Bluetooth-entitled python (python-ble.entitlements), see install.sh.
"""
from __future__ import annotations

import argparse
import ctypes
import os
import select
import socket
import sys
import time
import traceback

from rubicon.objc import NSObject, ObjCClass, objc_method
from rubicon.objc.runtime import load_library

VERSION = "4"

load_library("CoreBluetooth")
CBCentralManager = ObjCClass("CBCentralManager")
CBUUID = ObjCClass("CBUUID")
NSData = ObjCClass("NSData")
NSArray = ObjCClass("NSArray")
NSRunLoop = ObjCClass("NSRunLoop")
NSDate = ObjCClass("NSDate")
NSUUID = ObjCClass("NSUUID")

SVC = "FFB0"
TX = "FFB1"
NOTIFY_CHARS = ("FFB2", "FFB5")
CB_POWERED_ON = 5
WRITE_WITHOUT_RESPONSE = 1


def log(*a) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def nsdata_bytes(d) -> bytes:
    if d is None or d.length == 0:
        return b""
    return ctypes.string_at(d.bytes, d.length)


def nsnumber_int(n) -> int:
    """CoreBluetooth hands RSSI over as an NSNumber (an ObjCInstance under rubicon), not a Python int."""
    if isinstance(n, (int, float)):
        return int(n)
    try:
        return int(n.integerValue)
    except Exception:  # noqa: BLE001
        return int(str(n))


def to_nsdata(b: bytes):
    buf = ctypes.create_string_buffer(b, len(b))
    return NSData.dataWithBytes_length_(buf, len(b))


def svc_array():
    return NSArray.arrayWithObject_(CBUUID.UUIDWithString_(SVC))


def prevent_idle_sleep(reason: str) -> int | None:
    """Hold an IOKit 'PreventUserIdleSystemSleep' assertion for the life of the process.

    A locked iPhone suspends the CPU between radio events: our 20 s keep-alive timer then never fires,
    the heater drops the idle link after ~60 s (CBError 7) and the reconnect wakes us again — a
    connect/disconnect every minute (seen 2026-11-10). The app server's scheduler sleeps too.
    Returns the assertion id, or None if it could not be created."""
    try:
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        fn = iokit.IOPMAssertionCreateWithName
        fn.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        fn.restype = ctypes.c_int
        utf8 = 0x08000100
        aid = ctypes.c_uint32(0)
        rc = fn(cf.CFStringCreateWithCString(None, b"PreventUserIdleSystemSleep", utf8), 255,
                cf.CFStringCreateWithCString(None, reason.encode(), utf8), ctypes.byref(aid))
        if rc != 0:
            log(f"power assertion failed: {rc & 0xffffffff:#x}")
            return None
        return aid.value
    except Exception as e:  # noqa: BLE001
        log("power assertion unavailable:", e)
        return None


class Client:
    def __init__(self, sock: socket.socket, addr, authed: bool):
        self.sock, self.addr, self.authed, self.rbuf = sock, addr, authed, b""


class Pipe:
    def __init__(self, args):
        self.args = args
        self.state = "unpaired"
        self.central = None
        self.periph = None
        self.tx = None
        self.last_write = 0.0
        self.released_until = 0.0
        self.scan_until = 0.0          # pairing scan deadline (0 = not scanning for pairing)
        self.scan_seen: set[str] = set()
        self.last_notify: str | None = None
        self.last_info: str | None = None
        self.last_rssi: str | None = None
        self.rssi_at = 0.0
        self.paired = self.load_id()
        self.clients: list[Client] = []
        self.keepalive = bytes.fromhex(args.keepalive_hex) if args.keepalive_hex else b""
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind((args.bind, args.port))
        self.srv.listen(8)
        self.srv.setblocking(False)
        log(f"listening on {args.bind}:{args.port}, paired={self.paired or '-'}")

    # ---------- persistence ----------
    def load_id(self) -> str | None:
        try:
            with open(self.args.id_file, encoding="utf-8") as f:
                return f.read().strip() or None
        except OSError:
            return None

    def save_id(self, ident: str | None) -> None:
        self.paired = ident
        try:
            if ident:
                with open(self.args.id_file, "w", encoding="utf-8") as f:
                    f.write(ident)
            elif os.path.exists(self.args.id_file):
                os.remove(self.args.id_file)
        except OSError as e:
            log("cannot persist pairing:", e)
        self.broadcast(f"PAIRED {self.paired or '-'}")

    # ---------- state machine ----------
    def set_state(self, st: str) -> None:
        if st != self.state:
            self.state = st
            log("STATE", st)
            self.broadcast(f"STATE {st}")

    def powered(self) -> bool:
        return self.central is not None and int(self.central.state) == CB_POWERED_ON

    def go(self) -> None:
        """(Re)establish the connection to the paired device, or idle if unpaired."""
        self.tx = None
        if not self.powered():
            return
        if not self.paired:
            self.set_state("unpaired")
            self.update_scan()
            return
        uuid = NSUUID.alloc().initWithUUIDString_(self.paired)
        found = self.central.retrievePeripheralsWithIdentifiers_(NSArray.arrayWithObject_(uuid)) if uuid else None
        if found is not None and len(found) > 0:
            self.connect(found.objectAtIndex_(0))
        else:
            # iOS forgot the peripheral (e.g. after a Bluetooth reset): find it by scanning
            self.set_state("searching")
            self.update_scan()

    def connect(self, periph) -> None:
        self.periph = periph
        periph.delegate = DELEGATE
        self.set_state("connecting")
        # No timeout on purpose: iOS keeps the request pending and completes it when the device appears.
        self.central.connectPeripheral_options_(periph, None)

    def update_scan(self) -> None:
        """Scan while pairing-scan is active or while searching for the paired device."""
        if not self.powered():
            return
        want = self.scan_until > time.time() or self.state == "searching"
        if want:
            self.central.scanForPeripheralsWithServices_options_(svc_array(), None)
        else:
            self.central.stopScan()

    def disconnect(self) -> None:
        if self.periph is not None and self.central is not None:
            self.central.cancelPeripheralConnection_(self.periph)
        self.periph = None
        self.tx = None

    def write(self, data: bytes) -> bool:
        if self.state != "connected" or self.tx is None:
            return False
        self.periph.writeValue_forCharacteristic_type_(to_nsdata(data), self.tx, WRITE_WITHOUT_RESPONSE)
        self.last_write = time.time()
        if self.args.debug or data != self.keepalive:
            log("WRITE", data.hex())
        return True

    # ---------- periodic ----------
    def tick(self) -> None:
        now = time.time()
        if self.state == "released" and now >= self.released_until:
            log("release period over")
            self.go()
        elif self.state == "connected" and self.keepalive and now - self.last_write >= self.args.keepalive:
            self.write(self.keepalive)
        if self.state == "connected" and self.periph is not None and now - self.rssi_at >= 30:
            self.rssi_at = now
            self.periph.readRSSI()
        if self.scan_until and now >= self.scan_until:
            self.scan_until = 0.0
            self.update_scan()
            self.broadcast("SCANDONE")
        self.poll_sockets()

    # ---------- TCP ----------
    def broadcast(self, line: str) -> None:
        for c in list(self.clients):
            if c.authed:
                self.send(c, line)

    def send(self, c: Client, line: str) -> None:
        try:
            c.sock.sendall((line + "\n").encode())
        except OSError:
            self.drop(c)

    def drop(self, c: Client) -> None:
        if c in self.clients:
            self.clients.remove(c)
            log("client gone", c.addr)
        try:
            c.sock.close()
        except OSError:
            pass

    def greet(self, c: Client) -> None:
        self.send(c, f"HELLO blepipe {VERSION}")
        self.send(c, f"PAIRED {self.paired or '-'}")
        self.send(c, f"STATE {self.state}")
        if self.state == "connected":
            for line in (self.last_info, self.last_rssi, self.last_notify):
                if line:
                    self.send(c, line)

    def poll_sockets(self) -> None:
        r, _, _ = select.select([self.srv] + [c.sock for c in self.clients], [], [], 0)
        for s in r:
            if s is self.srv:
                cs, addr = self.srv.accept()
                cs.settimeout(2)
                c = Client(cs, addr, authed=not self.args.token)
                self.clients.append(c)
                log("client", addr)
                if c.authed:
                    self.greet(c)
                continue
            c = next(x for x in self.clients if x.sock is s)
            try:
                data = s.recv(4096)
            except OSError:
                data = b""
            if not data:
                self.drop(c)
                continue
            c.rbuf += data
            while b"\n" in c.rbuf:
                line, c.rbuf = c.rbuf.split(b"\n", 1)
                self.handle(c, line.decode("utf-8", "replace").strip())
            if len(c.rbuf) > 4096:
                self.drop(c)

    def handle(self, c: Client, line: str) -> None:
        if not line:
            return
        cmd, _, arg = line.partition(" ")
        cmd = cmd.upper()
        arg = arg.strip()
        if not c.authed:
            if cmd == "AUTH" and arg == self.args.token:
                c.authed = True
                self.greet(c)
            else:
                self.send(c, "ERR auth")
                self.drop(c)
            return
        if cmd == "PING":
            self.send(c, "PONG")
        elif cmd == "QUERYSTATE":
            self.send(c, f"PAIRED {self.paired or '-'}")
            self.send(c, f"STATE {self.state}")
        elif cmd == "WRITE":
            try:
                data = bytes.fromhex(arg)
            except ValueError:
                return self.send(c, "ERR bad-hex")
            if not 0 < len(data) <= 64:
                self.send(c, "ERR length")
            elif self.write(data):
                self.send(c, "OK")
            else:
                self.send(c, f"ERR not-connected {self.state}")
        elif cmd == "SCAN":
            try:
                secs = max(3.0, min(float(arg or 15), 60.0))
            except ValueError:
                return self.send(c, "ERR bad-seconds")
            self.scan_until = time.time() + secs
            self.scan_seen.clear()
            self.update_scan()
            self.send(c, "OK")
        elif cmd == "PAIR":
            if NSUUID.alloc().initWithUUIDString_(arg) is None:
                return self.send(c, "ERR bad-uuid")
            self.disconnect()
            self.save_id(arg.upper())
            self.scan_until = 0.0
            self.go()
            self.send(c, "OK")
        elif cmd == "FORGET":
            self.disconnect()
            self.save_id(None)
            self.last_notify = None
            self.go()
            self.send(c, "OK")
        elif cmd == "RELEASE":
            try:
                secs = max(1.0, min(float(arg or 60), 3600.0))
            except ValueError:
                return self.send(c, "ERR bad-seconds")
            self.released_until = time.time() + secs
            self.disconnect()
            self.set_state("released")
            self.send(c, "OK")
        else:
            self.send(c, "ERR unknown-command")


PIPE: Pipe | None = None
DELEGATE = None


def guarded(fn):
    def wrapper(*a):
        try:
            fn(*a)
        except Exception:  # noqa: BLE001 — never let an exception escape into ObjC
            log("callback error:\n" + traceback.format_exc())
    return wrapper


@guarded
def _did_update_state(central) -> None:
    st = int(central.state)
    log("central state", st)
    if st == CB_POWERED_ON:
        PIPE.go()
    else:
        PIPE.tx = None
        PIPE.set_state("unpaired" if not PIPE.paired else "searching")


@guarded
def _did_discover(periph, adv, rssi) -> None:
    ident = str(periph.identifier.UUIDString)
    mfr = adv.objectForKey_("kCBAdvDataManufacturerData") if adv is not None else None
    mfr_hex = nsdata_bytes(mfr).hex() if mfr is not None else "-"
    if PIPE.args.debug:
        log("adv", periph.name, ident, rssi, mfr_hex)
    if PIPE.scan_until and ident not in PIPE.scan_seen:
        name = str(periph.name) if periph.name is not None else "-"
        PIPE.broadcast(f"DEVICE {ident} {nsnumber_int(rssi)} {mfr_hex} {name}")
        PIPE.scan_seen.add(ident)   # only after a successful report, so a failure is retried next advert
    if PIPE.state == "searching" and ident == PIPE.paired:
        log("found paired device by scan")
        PIPE.connect(periph)
        PIPE.update_scan()


@guarded
def _did_connect(periph) -> None:
    log("BLE connected, discovering")
    periph.discoverServices_(svc_array())


@guarded
def _did_disconnect(periph, err) -> None:
    log("BLE disconnected", err if err is not None else "")
    PIPE.tx = None
    if PIPE.state in ("released", "unpaired") or PIPE.periph is None:
        return
    if str(periph.identifier.UUIDString) == PIPE.paired:
        PIPE.connect(periph)      # pending reconnect


@guarded
def _did_discover_services(periph, err) -> None:
    for s in periph.services or []:
        if str(s.UUID.UUIDString) == SVC:
            periph.discoverCharacteristics_forService_(None, s)


@guarded
def _did_discover_chars(periph, svc, err) -> None:
    for ch in svc.characteristics or []:
        u = str(ch.UUID.UUIDString)
        if u == TX:
            PIPE.tx = ch
        if u in NOTIFY_CHARS:
            periph.setNotifyValue_forCharacteristic_(True, ch)


@guarded
def _did_update_notify_state(periph, ch, err) -> None:
    if str(ch.UUID.UUIDString) == NOTIFY_CHARS[0] and PIPE.tx is not None and err is None:
        PIPE.set_state("connected")
        PIPE.last_write = time.time()
        name = str(periph.name) if periph.name is not None else "-"
        PIPE.last_info = f"INFO {periph.identifier.UUIDString} {name}"
        PIPE.broadcast(PIPE.last_info)
        PIPE.rssi_at = 0.0          # read signal strength right away


@guarded
def _did_update_value(periph, ch, err) -> None:
    line = f"NOTIFY {ch.UUID.UUIDString} {nsdata_bytes(ch.value).hex()}"
    log(line)
    if str(ch.UUID.UUIDString) == NOTIFY_CHARS[0]:
        PIPE.last_notify = line
    PIPE.broadcast(line)


@guarded
def _did_read_rssi(rssi, err) -> None:
    if err is None and rssi is not None:
        PIPE.last_rssi = f"RSSI {nsnumber_int(rssi)}"
        PIPE.broadcast(PIPE.last_rssi)


class Delegate(NSObject):
    @objc_method
    def centralManagerDidUpdateState_(self, central) -> None:
        _did_update_state(central)

    @objc_method
    def centralManager_didDiscoverPeripheral_advertisementData_RSSI_(self, central, periph, adv, rssi) -> None:
        _did_discover(periph, adv, rssi)

    @objc_method
    def centralManager_didConnectPeripheral_(self, central, periph) -> None:
        _did_connect(periph)

    @objc_method
    def centralManager_didFailToConnectPeripheral_error_(self, central, periph, err) -> None:
        _did_disconnect(periph, err)

    @objc_method
    def centralManager_didDisconnectPeripheral_error_(self, central, periph, err) -> None:
        _did_disconnect(periph, err)

    @objc_method
    def peripheral_didDiscoverServices_(self, periph, err) -> None:
        _did_discover_services(periph, err)

    @objc_method
    def peripheral_didDiscoverCharacteristicsForService_error_(self, periph, svc, err) -> None:
        _did_discover_chars(periph, svc, err)

    @objc_method
    def peripheral_didUpdateNotificationStateForCharacteristic_error_(self, periph, ch, err) -> None:
        _did_update_notify_state(periph, ch, err)

    @objc_method
    def peripheral_didUpdateValueForCharacteristic_error_(self, periph, ch, err) -> None:
        _did_update_value(periph, ch, err)

    @objc_method
    def peripheral_didReadRSSI_error_(self, periph, rssi, err) -> None:
        _did_read_rssi(rssi, err)


def main() -> int:
    global PIPE, DELEGATE
    ap = argparse.ArgumentParser()
    ap.add_argument("--bind", default="127.0.0.1", help="use 0.0.0.0 to expose the raw pipe on the LAN")
    ap.add_argument("--port", type=int, default=7700)
    ap.add_argument("--token", default="", help="require 'AUTH <token>' from clients")
    ap.add_argument("--keepalive", type=float, default=20.0)
    ap.add_argument("--keepalive-hex", default="aa0300ff000255")
    ap.add_argument("--id-file", default="data/paired-device")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--allow-sleep", action="store_true", help="don't hold the prevent-idle-sleep assertion")
    args = ap.parse_args()
    os.makedirs(os.path.dirname(args.id_file) or ".", exist_ok=True)
    if not args.allow_sleep:
        aid = prevent_idle_sleep("blepipe: keep the heater link alive")
        log(f"holding idle-sleep assertion {aid}" if aid is not None else "WARNING: phone may sleep; link will flap")

    PIPE = Pipe(args)
    DELEGATE = Delegate.alloc().init()
    PIPE.central = CBCentralManager.alloc().initWithDelegate_queue_(DELEGATE, None)
    loop = NSRunLoop.currentRunLoop
    log(f"blepipe {VERSION} started")
    while True:
        loop.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.05))
        try:
            PIPE.tick()
        except Exception:  # noqa: BLE001
            log("tick error:\n" + traceback.format_exc())


if __name__ == "__main__":
    sys.exit(main())
