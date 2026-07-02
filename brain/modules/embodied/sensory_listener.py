"""Sensory Loop: turns StackChan sensor/status events into logged,
agent-readable context, and dispatches them to skill reactions.

Implements the "Perception-to-Cognition Pipeline" described in
specs/phase_04_embodied_soul/sensory_integration_spec.md.
"""

import json
import os
from datetime import datetime

from . import config

SENSOR_TOPICS = [
    "sensor/button",
    "sensor/touch",
    "sensor/imu",
    "sensor/vision",
    "status/heartbeat",
]


class SensoryListener:
    def __init__(self, bridge):
        self.bridge = bridge
        self._reactions = {}  # topic_suffix -> [callback(payload)]

        for topic_suffix in SENSOR_TOPICS:
            bridge.on(topic_suffix, self._make_handler(topic_suffix))

    def on_event(self, topic_suffix, callback):
        """Register a skill reaction for a sensor/status topic suffix."""
        self._reactions.setdefault(topic_suffix, []).append(callback)

    def _make_handler(self, topic_suffix):
        def handler(payload, full_topic):
            self._log_event(topic_suffix, payload)
            for callback in self._reactions.get(topic_suffix, []):
                callback(payload)
        return handler

    def _log_event(self, topic_suffix, payload):
        os.makedirs(os.path.dirname(config.EVENTS_LOG_PATH), exist_ok=True)
        record = {
            "timestamp": datetime.now().isoformat(),
            "topic": topic_suffix,
            "payload": payload,
        }
        with open(config.EVENTS_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
