# utils/mqtt_pub.py — MQTT publisher with auto-reconnect

import json, time
import paho.mqtt.client as mqtt
from config import (
    MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASS,
    TOPIC_CONTROL, TOPIC_COUNT_FMT, TOPIC_SIREN_FMT,
)


class MQTTPublisher:
    def __init__(self):
        self._client = mqtt.Client()
        self._client.username_pw_set(MQTT_USER, MQTT_PASS)
        self._client.tls_set()
        self._client.tls_insecure_set(True)
        self._client.on_disconnect = self._on_disconnect
        self._connected = False
        self._connect()

    def _connect(self):
        while not self._connected:
            try:
                self._client.connect(MQTT_HOST, MQTT_PORT)
                self._client.loop_start()
                self._connected = True
                print("[MQTT] Connected.\n")
            except Exception as e:
                print(f"[MQTT] Failed: {e}. Retrying in 5s ...")
                time.sleep(5)

    def _on_disconnect(self, client, userdata, rc):
        self._connected = False
        print(f"[MQTT] Disconnected (rc={rc}). Reconnecting ...")
        self._connect()

    def _pub(self, topic, payload):
        try:
            self._client.publish(topic, payload)
        except Exception as e:
            print(f"[MQTT] Publish error: {e}")

    def publish_counts(self, counts: dict):
        for side, cnt in counts.items():
            self._pub(TOPIC_COUNT_FMT.format(side), str(cnt))

    def publish_siren(self, side: int, score: float, active: bool):
        self._pub(TOPIC_SIREN_FMT.format(side),
                  json.dumps({"side": side, "score": round(score, 3), "active": active}))

    def publish_control(self, decision: dict):
        self._pub(TOPIC_CONTROL, json.dumps({
            "open_side"     : decision["open_side"],
            "green_time"    : decision["green_time"],
            "siren_override": decision["siren_override"],
            "siren_sides"   : decision["siren_sides"],
        }))

    def disconnect(self):
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            pass