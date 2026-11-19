# Roadmap

## TODO: Home Assistant integration
Goal: the heater shows up in Home Assistant as a climate device, controllable from HA dashboards and
automations, while the control plane keeps owning the schedule and safety checks.

Current state: `control_plane/mqtt_bridge.py` is a **draft** — written against the HA MQTT discovery schema
but never run against a real broker, not deployed by default and without tests. It already publishes
discovery for a climate entity (off/heat, 18–35 °C, `eco` preset), a voice-control switch, an off-timer
number and a "command rejected" problem sensor, with a retained state topic and an LWT availability topic.

### Phase 1 — MQTT discovery (use the existing draft)
- [ ] Run a broker (Mosquitto add-on in HA, or Mosquitto on the router / home server).
- [ ] Make `paho-mqtt` available to the control plane (`lib/` on the phone, or `pip install` when
      the control plane runs elsewhere); add it to `requirements.txt` as an optional extra.
- [ ] Start the server with `--mqtt <host>:1883 [--mqtt-user U]` and the password in `HEATER_MQTT_PASS`
      (iPhone backend: add the args to the `com.ble2remote.app` daemon in `backends/ios/install.sh`, behind an
      opt-in installer flag).
- [ ] Verify in HA: entities appear under one device, state follows changes made from the web app, the IR
      remote and HA itself; availability goes `offline` when the BLE link drops or the server stops.
- [ ] Rename the timer entity, or split it: while the heater is off the same command is an **on-timer**
      (see [protocol.md](protocol.md)); HA should not label it "Off-timer" then.
- [ ] Expose read-only sensors: BLE link state, RSSI, last seen, "on since", next scheduled event.
- [ ] Surface child-lock rejections to HA (the problem sensor) and decide when it resets.
- [ ] Tests for `mqtt_bridge.py` with a fake MQTT client (discovery payloads, command topic mapping,
      refused commands).
- [ ] Document the setup in a `docs/home-assistant.md`.

### Phase 2 — schedule and safety in HA
- [ ] Decide who owns schedules when both exist: keep the control-plane scheduler as the source of truth
      and expose "pause schedule" / "skip next event" as HA switches/buttons, rather than duplicating
      schedules as HA automations.
- [ ] Mark HA-originated changes as manual overrides (hold until the next event), like web-app changes.
- [ ] Keep the maximum on-time safety check in the control plane so it applies to HA commands too.

### Phase 3 — optional
- [ ] Native HA custom integration (HACS) talking to the REST API, as an alternative to MQTT for users
      without a broker.
- [ ] Run the control plane as an HA add-on (Docker image) with the BLE link on a separate device.
- [ ] Matter bridge from HA to other ecosystems (e.g. SmartThings).

## BLE link backends
- **ESP32**: an ESP32 module that speaks the same pipe protocol
  ([architecture.md](architecture.md#pipe-protocol)); the control plane is unchanged. Removes the
  jailbreak / manual re-activation after reboot.
- **PC / Raspberry Pi with bleak**: a `blepipe` implementation on top of bleak for Linux, Windows and
  macOS, so the whole stack can run on a single small computer within BLE range.

## Open protocol questions
- Meaning of status byte LV (2 while power-save) and commands FE/F8; anything on FFB5.
