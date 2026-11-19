"""Spike: can a plain process on the jailbroken iPhone use CoreBluetooth?

Scans for service FFB0, connects to the heater, subscribes FFB2, sends a status
query, prints the reply, disconnects. Read-only (query only).

  PYTHONPATH=/var/mobile/blebridge/lib python3 ble_spike.py
"""
import ctypes
import sys
import time

from rubicon.objc import NSObject, ObjCClass, objc_method
from rubicon.objc.runtime import load_library

load_library("CoreBluetooth")
CBCentralManager = ObjCClass("CBCentralManager")
CBUUID = ObjCClass("CBUUID")
NSData = ObjCClass("NSData")
NSArray = ObjCClass("NSArray")
NSRunLoop = ObjCClass("NSRunLoop")
NSDate = ObjCClass("NSDate")

SVC = CBUUID.UUIDWithString_("FFB0")
TX = "FFB1"
RX = "FFB2"
QUERY = bytes.fromhex("aa030200000555")
STATES = {0: "unknown", 1: "resetting", 2: "unsupported", 3: "unauthorized", 4: "poweredOff", 5: "poweredOn"}

done = False
keep = {}  # strong refs so ObjC objects are not released


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def nsdata_bytes(d) -> bytes:
    if d is None or d.length == 0:
        return b""
    return ctypes.string_at(d.bytes, d.length)


class Spike(NSObject):
    @objc_method
    def centralManagerDidUpdateState_(self, central) -> None:
        st = int(central.state)
        log("central state:", STATES.get(st, st))
        if st == 5:
            central.scanForPeripheralsWithServices_options_(NSArray.arrayWithObject_(SVC), None)
            log("scanning for FFB0 ...")

    @objc_method
    def centralManager_didDiscoverPeripheral_advertisementData_RSSI_(self, central, periph, adv, rssi) -> None:
        log("found", periph.name, periph.identifier.UUIDString, "rssi", rssi)
        central.stopScan()
        keep["periph"] = periph
        periph.delegate = self
        central.connectPeripheral_options_(periph, None)

    @objc_method
    def centralManager_didConnectPeripheral_(self, central, periph) -> None:
        log("connected")
        periph.discoverServices_(NSArray.arrayWithObject_(SVC))

    @objc_method
    def centralManager_didFailToConnectPeripheral_error_(self, central, periph, err) -> None:
        log("connect failed:", err)
        global done
        done = True

    @objc_method
    def peripheral_didDiscoverServices_(self, periph, err) -> None:
        for s in periph.services:
            log("service", s.UUID.UUIDString)
            periph.discoverCharacteristics_forService_(None, s)

    @objc_method
    def peripheral_didDiscoverCharacteristicsForService_error_(self, periph, svc, err) -> None:
        tx = None
        for c in svc.characteristics:
            u = str(c.UUID.UUIDString)
            log("  char", u, "props", int(c.properties))
            if u == RX:
                periph.setNotifyValue_forCharacteristic_(True, c)
            if u == TX:
                tx = c
        if tx is not None:
            buf = ctypes.create_string_buffer(QUERY, len(QUERY))
            data = NSData.dataWithBytes_length_(buf, len(QUERY))
            periph.writeValue_forCharacteristic_type_(data, tx, 1)  # 1 = without response
            log("WRITE", QUERY.hex())

    @objc_method
    def peripheral_didUpdateValueForCharacteristic_error_(self, periph, ch, err) -> None:
        log("NOTIFY", ch.UUID.UUIDString, nsdata_bytes(ch.value).hex())
        global done
        done = True


def main() -> int:
    delegate = Spike.alloc().init()
    central = CBCentralManager.alloc().initWithDelegate_queue_(delegate, None)
    deadline = time.time() + 30
    loop = NSRunLoop.currentRunLoop
    while not done and time.time() < deadline:
        loop.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.1))
    p = keep.get("periph")
    if p is not None:
        central.cancelPeripheralConnection_(p)
        loop.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.5))
    log("SUCCESS" if done else "TIMEOUT")
    return 0 if done else 1


if __name__ == "__main__":
    sys.exit(main())
