"""OPTIONAL, DRAFT: MQTT bridge with Home Assistant discovery (untested, not deployed by default; see docs/roadmap.md).

Enable with  server.py --mqtt host:1883 [--mqtt-user U --mqtt-pass P]  (needs paho-mqtt in lib/).
Topics: heater/<id>/state (JSON, retained), heater/<id>/availability, heater/<id>/{mode,setpoint,preset,voice,timer}/set
"""
from __future__ import annotations

import json

import protocol as p
from heater import log


def start(heater, args, version: str) -> None:
    import paho.mqtt.client as mqtt

    dev = args.device_id
    base = f"heater/{dev}"
    avail = f"{base}/availability"
    device = {"identifiers": [f"ble2remote_{dev}"], "name": "Heater", "manufacturer": "AirMeter",
              "model": "UAF028 baseboard heater", "sw_version": f"ble2remote {version}"}
    common = {"availability_topic": avail, "device": device}
    state_t = f"{base}/state"
    configs = {
        f"homeassistant/climate/{dev}/config": {
            **common, "name": None, "unique_id": f"{dev}_climate", "modes": ["off", "heat"],
            "mode_command_topic": f"{base}/mode/set", "mode_state_topic": state_t,
            "mode_state_template": "{{ value_json.mode }}",
            "temperature_command_topic": f"{base}/setpoint/set", "temperature_state_topic": state_t,
            "temperature_state_template": "{{ value_json.setpoint }}",
            "preset_modes": ["eco"], "preset_mode_command_topic": f"{base}/preset/set",
            "preset_mode_state_topic": state_t, "preset_mode_value_template": "{{ value_json.preset }}",
            "min_temp": p.TEMP_MIN, "max_temp": p.TEMP_MAX, "temp_step": 1, "temperature_unit": "C",
        },
        f"homeassistant/switch/{dev}_voice/config": {
            **common, "name": "Voice control", "unique_id": f"{dev}_voice", "command_topic": f"{base}/voice/set",
            "state_topic": state_t, "value_template": "{{ 'ON' if value_json.voice else 'OFF' }}",
        },
        f"homeassistant/number/{dev}_timer/config": {
            **common, "name": "Off-timer", "unique_id": f"{dev}_timer", "command_topic": f"{base}/timer/set",
            "state_topic": state_t, "value_template": "{{ value_json.timer_hours }}",
            "min": 0, "max": p.TIMER_MAX, "step": 1, "unit_of_measurement": "h", "mode": "box",
        },
        f"homeassistant/binary_sensor/{dev}_rejected/config": {
            **common, "name": "Command rejected", "unique_id": f"{dev}_rejected", "device_class": "problem",
            "state_topic": state_t, "value_template": "{{ 'ON' if value_json.rejected else 'OFF' }}",
        },
    }
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"ble2remote-{dev}")
    except AttributeError:
        client = mqtt.Client(client_id=f"ble2remote-{dev}")
    if args.mqtt_user:
        client.username_pw_set(args.mqtt_user, args.mqtt_pass)
    client.will_set(avail, "offline", retain=True)

    def publish(st: dict) -> None:
        client.publish(avail, "online" if st["available"] else "offline", retain=True)
        if st["available"]:
            client.publish(state_t, json.dumps(st), retain=True)

    def on_connect(c, *_):
        for topic, cfg in configs.items():
            c.publish(topic, json.dumps(cfg), retain=True)
        c.subscribe(f"{base}/+/set")
        publish(heater.state())

    def on_message(c, _u, msg):
        what, val = msg.topic.split("/")[-2], msg.payload.decode("utf-8", "replace").strip()
        m = {"mode": lambda: {"power": val == "heat"}, "setpoint": lambda: {"setpoint": val},
             "preset": lambda: {"eco": val == "eco"}, "voice": lambda: {"voice": val.upper() == "ON"},
             "timer": lambda: {"timer_hours": val}}
        try:
            if what in m:
                heater.submit(m[what](), source="mqtt")
        except ValueError as e:
            log("mqtt command refused:", e)

    client.on_connect = on_connect
    client.on_message = on_message
    heater.on_change(publish)
    host, _, port = args.mqtt.partition(":")
    client.connect_async(host, int(port or 1883), keepalive=30)
    client.loop_start()
    log("mqtt enabled", args.mqtt)
