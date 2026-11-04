"""MQTT transport layer between Hermes (Mac) and StackChan (ESP32).

This is the single point of contact with the MQTT broker. Everything
else (command_mapper, sensory_listener, skills) talks to StackChan
through a `MQTTBridge` instance instead of touching paho-mqtt directly.
"""

import json

import paho.mqtt.client as mqtt

from . import config


class MQTTBridge:
    def __init__(self, on_connect=None, client_id=None):
        self._on_connect_callback = on_connect
        self._handlers = {}  # topic_suffix -> [callback(payload_dict)]

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id or config.MQTT_CLIENT_ID,
        )
        self.client.on_connect = self._handle_connect
        self.client.on_message = self._handle_message

    def connect(self):
        print(f"🔌 [MQTTBridge] 連線到 {config.MQTT_HOST}:{config.MQTT_PORT} ...")
        self.client.connect(config.MQTT_HOST, config.MQTT_PORT, keepalive=60)

    def loop_forever(self):
        self.client.loop_forever()

    def loop_start(self):
        self.client.loop_start()

    def loop_stop(self):
        self.client.loop_stop()

    def publish(self, topic_suffix, payload: dict):
        full_topic = config.topic(topic_suffix)
        message = json.dumps(payload, ensure_ascii=False)
        self.client.publish(full_topic, message)
        print(f"📤 [MQTTBridge] {full_topic} -> {message}")

    def on(self, topic_suffix, callback):
        """Register a callback(payload_dict, full_topic) for a sensor/status topic suffix."""
        self._handlers.setdefault(topic_suffix, []).append(callback)
        if self.client.is_connected():
            self.client.subscribe(config.topic(topic_suffix))

    def _handle_connect(self, client, userdata, flags, reason_code, properties):
        print(f"✅ [MQTTBridge] 已連線 (reason_code={reason_code})")
        for topic_suffix in self._handlers:
            client.subscribe(config.topic(topic_suffix))
        if self._on_connect_callback:
            self._on_connect_callback(self)

    def _handle_message(self, client, userdata, msg):
        prefix = f"{config.TOPIC_PREFIX}/"
        if not msg.topic.startswith(prefix):
            return
        topic_suffix = msg.topic[len(prefix):]

        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {"raw": msg.payload.decode("utf-8", errors="replace")}

        print(f"📥 [MQTTBridge] {msg.topic} <- {payload}")

        for callback in self._handlers.get(topic_suffix, []):
            callback(payload, msg.topic)
