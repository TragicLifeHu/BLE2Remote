#!/bin/sh
# Wait for WeChat to start on the phone, then attach the CoreBluetooth hook (quiet, runs until killed).
# Requires backends/ios/tools/ssh_tunnel.py running.  Usage: sh re/tools/capture_wechat.sh re/captures/ble-XXX.log
OUT="$1"; H=127.0.0.1:27042; PY=.venv/Scripts
echo "waiting for WeChat..."
while :; do
  PID=$($PY/frida-ps -H $H 2>/dev/null | awk '$2=="WeChat"{print $1; exit}')
  [ -n "$PID" ] && break
  sleep 1
done
echo "WeChat pid $PID, attaching"
exec $PY/frida -H $H -p "$PID" -l re/frida/hook_corebluetooth.js -o "$OUT" -q -t inf
