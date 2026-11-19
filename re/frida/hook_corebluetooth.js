// Frida hook: log all CoreBluetooth traffic inside WeChat (Mini Program BLE relay).
// Usage (from PC, frida-server running on the iPhone, USB connected):
//   frida -U -n WeChat -l re/frida/hook_corebluetooth.js -o re/captures/session-XX.log
// Output lines:  <time> <EVENT> key=value ...   (hex payloads are lowercase, no spaces)
'use strict';

const ts = () => new Date().toISOString().substring(11, 23);
const log = (ev, s) => console.log(`${ts()} ${ev} ${s}`);

function hexOf(ptrData) {
  if (ptrData.isNull()) return '';
  const d = new ObjC.Object(ptrData);
  const n = d.length();
  if (n === 0) return '';
  const u8 = new Uint8Array(d.bytes().readByteArray(n));
  return Array.from(u8, b => b.toString(16).padStart(2, '0')).join('');
}
const str = o => (o === null || o.isNull?.()) ? '-' : new ObjC.Object(o).toString();
function charDesc(chPtr) {
  const ch = new ObjC.Object(chPtr);
  const svc = ch.service();
  return `svc=${svc ? svc.UUID().UUIDString() : '?'} char=${ch.UUID().UUIDString()}`;
}
function periphDesc(pPtr) {
  const p = new ObjC.Object(pPtr);
  return `periph=${p.identifier().UUIDString()} name=${str(p.name())}`;
}

// ---- Outgoing (app -> device) ----
const CBP = ObjC.classes.CBPeripheral;
Interceptor.attach(CBP['- writeValue:forCharacteristic:type:'].implementation, {
  onEnter(a) {
    log('WRITE', `${charDesc(a[3])} type=${a[4].toInt32() === 0 ? 'resp' : 'noresp'} data=${hexOf(a[2])}`);
  }
});
Interceptor.attach(CBP['- setNotifyValue:forCharacteristic:'].implementation, {
  onEnter(a) { log('SUBSCRIBE', `${charDesc(a[3])} on=${a[2].toInt32()}`); hookPeripheralDelegate(this, a[0]); }
});
Interceptor.attach(CBP['- readValueForCharacteristic:'].implementation, {
  onEnter(a) { log('READ_REQ', charDesc(a[2])); hookPeripheralDelegate(this, a[0]); }
});
if (CBP['- writeValue:forDescriptor:']) {
  Interceptor.attach(CBP['- writeValue:forDescriptor:'].implementation, {
    onEnter(a) { log('WRITE_DESC', `desc=${new ObjC.Object(a[3]).UUID().UUIDString()} data=${hexOf(a[2])}`); }
  });
}

// ---- Central: scan / connect / discovery ----
const CBC = ObjC.classes.CBCentralManager;
Interceptor.attach(CBC['- scanForPeripheralsWithServices:options:'].implementation, {
  onEnter(a) { log('SCAN', `services=${str(a[2]).replace(/\s+/g, ' ')} options=${str(a[3]).replace(/\s+/g, ' ')}`); hookCentralDelegate(a[0]); }
});
Interceptor.attach(CBC['- connectPeripheral:options:'].implementation, {
  onEnter(a) { log('CONNECT', periphDesc(a[2])); hookPeripheralDelegate(this, a[2]); }
});
Interceptor.attach(CBC['- cancelPeripheralConnection:'].implementation, {
  onEnter(a) { log('DISCONNECT_REQ', periphDesc(a[2])); }
});

// ---- Incoming (device -> app): hook delegate classes lazily ----
const hookedClasses = new Set();
function hookMethod(cls, sel, cb) {
  const key = `${cls.$className}${sel}`;
  if (hookedClasses.has(key)) return;
  const m = cls[sel];
  if (!m) return;
  hookedClasses.add(key);
  Interceptor.attach(m.implementation, { onEnter: cb });
  log('HOOKED', `${cls.$className} ${sel}`);
}
function hookPeripheralDelegate(_ctx, pPtr) {
  const d = new ObjC.Object(pPtr).delegate();
  if (!d) return;
  const cls = d.$class;
  hookMethod(cls, '- peripheral:didUpdateValueForCharacteristic:error:', a => {
    const ch = new ObjC.Object(a[3]);
    const err = a[4].isNull() ? '' : ` error=${str(a[4])}`;
    log('NOTIFY', `${charDesc(a[3])} data=${hexOf(ch.value())}${err}`);
  });
  hookMethod(cls, '- peripheral:didWriteValueForCharacteristic:error:', a => {
    log('WRITE_ACK', `${charDesc(a[3])}${a[4].isNull() ? '' : ' error=' + str(a[4])}`);
  });
  hookMethod(cls, '- peripheral:didDiscoverCharacteristicsForService:error:', a => {
    const svc = new ObjC.Object(a[3]);
    const chars = svc.characteristics();
    const out = [];
    for (let i = 0; chars && i < chars.count(); i++) {
      const c = chars.objectAtIndex_(i);
      out.push(`${c.UUID().UUIDString()}(props=0x${c.properties().toString(16)})`);
    }
    log('CHARS', `svc=${svc.UUID().UUIDString()} ${out.join(' ')}`);
  });
}
const seen = new Map();
function hookCentralDelegate(cPtr) {
  const d = new ObjC.Object(cPtr).delegate();
  if (!d) return;
  hookMethod(d.$class, '- centralManager:didDiscoverPeripheral:advertisementData:RSSI:', a => {
    const p = periphDesc(a[3]);
    const adv = str(a[4]).replace(/\s+/g, ' ');
    if (seen.get(p) === adv) return;   // only log new/changed advertisements
    seen.set(p, adv);
    log('ADV', `${p} rssi=${str(a[5])} adv=${adv}`);
  });
  hookMethod(d.$class, '- centralManager:didConnectPeripheral:', a => log('CONNECTED', periphDesc(a[3])));
  hookMethod(d.$class, '- centralManager:didDisconnectPeripheral:error:', a => log('DISCONNECTED', periphDesc(a[3])));
}

// Mark the tap you are about to make from the REPL:  mark("power on")
globalThis.mark = (s) => log('MARK', s);
log('READY', 'CoreBluetooth hooks installed. Use mark("label") before each tap.');
