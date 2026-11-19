#!/bin/sh
# Install / update the heater bridge on a Dopamine-roothide iPhone (run as root from /var/mobile/ble2remote, see backends/ios/tools/deploy.py):
#   sudo sh install.sh [--http-port 8080] [--pipe-lan]
# Installs two launchd daemons:
#   com.ble2remote.blepipe  BLE <-> TCP pipe (Bluetooth-entitled python), 127.0.0.1:7700 by default
#   com.ble2remote.app      web app + scheduler on :8080
# Paths are jbroot-relative: roothide's launchctl translates them. launchd only spawns binaries that
# live inside the jbroot proper, hence the python copy in /usr/local/bin.
set -e
DIR=/var/mobile/ble2remote
HTTP_PORT=8080
PIPE_BIND=127.0.0.1
while [ $# -gt 0 ]; do
  case "$1" in
    --http-port) HTTP_PORT="$2"; shift 2;;
    --pipe-lan) PIPE_BIND=0.0.0.0; shift;;
    *) echo "unknown arg $1"; exit 1;;
  esac
done
cd "$DIR"
mkdir -p data logs lib

# --- migrate from the first prototype layout ---
OLD=/var/mobile/blebridge
if [ -d "$OLD" ]; then
  [ -f "$OLD/blepipe.peripheral" ] && [ ! -f data/paired-device ] && cp "$OLD/blepipe.peripheral" data/paired-device && echo "migrated pairing"
  [ -d "$OLD/lib/rubicon" ] && [ ! -d lib/rubicon ] && cp -R "$OLD/lib/." lib/ && echo "migrated lib"
fi
for old in com.ble2remote.airmeterd; do
  if [ -f "/Library/LaunchDaemons/$old.plist" ]; then
    launchctl unload "/Library/LaunchDaemons/$old.plist" 2>/dev/null || true
    rm -f "/Library/LaunchDaemons/$old.plist"
    echo "removed old daemon $old"
  fi
done
[ -d lib/rubicon ] || { echo "ERROR: lib/rubicon missing (upload rubicon-objc)"; exit 1; }

# --- Bluetooth-entitled python for blepipe ---
PY=/usr/local/bin/python3-ble
mkdir -p /usr/local/bin
cp "$(readlink -f /usr/bin/python3)" "$PY"
ldid -S"$DIR/python-ble.entitlements" "$PY"
chmod 755 "$PY"
chown -R mobile:mobile "$DIR"

plist() { # label program args...
  LABEL=$1; shift
  ARGS=""
  for a in "$@"; do ARGS="$ARGS    <string>$a</string>
"; done
  cat > "/Library/LaunchDaemons/$LABEL.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
$ARGS  </array>
  <key>EnvironmentVariables</key>
  <dict><key>PYTHONPATH</key><string>$DIR/lib</string></dict>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>UserName</key><string>mobile</string>
  <key>ProcessType</key><string>Interactive</string>
  <key>POSIXSpawnType</key><string>Interactive</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$DIR/logs/$LABEL.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/$LABEL.log</string>
</dict>
</plist>
EOF
  chown root:wheel "/Library/LaunchDaemons/$LABEL.plist"
  chmod 644 "/Library/LaunchDaemons/$LABEL.plist"
  launchctl unload "/Library/LaunchDaemons/$LABEL.plist" 2>/dev/null || true
  launchctl load -w "/Library/LaunchDaemons/$LABEL.plist"
}

# keep logs bounded across restarts
for f in logs/*.log; do [ -f "$f" ] && [ "$(wc -c < "$f")" -gt 2000000 ] && : > "$f"; done

plist com.ble2remote.blepipe "$PY" -u "$DIR/blepipe.py" --bind "$PIPE_BIND" --id-file "$DIR/data/paired-device"
plist com.ble2remote.app /usr/bin/python3 -u "$DIR/app/server.py" --http "0.0.0.0:$HTTP_PORT" --data "$DIR/data"

sleep 2
launchctl list | grep com.ble2remote || true
echo "installed. web app: http://<this-phone-ip>:$HTTP_PORT"
