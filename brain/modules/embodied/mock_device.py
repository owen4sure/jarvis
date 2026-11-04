"""Software stand-in for the StackChan ESP32, for testing before the
hardware arrives.

- Subscribes to all `cmd/*` topics and prints what a real device
  would do (blink LED, move servo, play audio URL, etc).
- Can publish fake sensor events (button/touch/imu) on demand.
- Responds to `sync/request` with a couple of fake buffered events,
  so offline_sync can be exercised end-to-end.

Run directly: `python -m modules.embodied.mock_device button`
              `python -m modules.embodied.mock_device touch`
              `python -m modules.embodied.mock_device shake`
              `python -m modules.embodied.mock_device vision`   (fake off-center face -> LOOK_AT)
              `python -m modules.embodied.mock_device vision_lost`  (fake "no face" -> HEAD_CENTER)
              `python -m modules.embodied.mock_device listen`   (just listens for cmd/*)
"""

import sys
import time

from . import config
from .mqtt_bridge import MQTTBridge

CMD_TOPICS = ["cmd/expression", "cmd/servo", "cmd/led", "cmd/audio"]


def _on_connect(bridge):
    for t in CMD_TOPICS:
        bridge.on(t, _make_cmd_printer(t))
    bridge.on("sync/request", _on_sync_request)


def _make_cmd_printer(topic_suffix):
    def handler(payload, full_topic):
        print(f"🤖 [MockDevice] 收到指令 {topic_suffix}: {payload}")
    return handler


def _on_sync_request(payload, full_topic):
    print("🤖 [MockDevice] 收到 sync/request，回傳離線緩衝事件...")
    bridge = _bridge
    bridge.publish("sync/buffer", {"timestamp": "2026-06-14T03:00:00", "event": "touch", "value": True})
    bridge.publish("sync/buffer", {"timestamp": "2026-06-14T03:05:00", "event": "button", "button": "A", "event_type": "press"})


_bridge = None


def main():
    global _bridge
    action = sys.argv[1] if len(sys.argv) > 1 else "listen"

    _bridge = MQTTBridge(on_connect=_on_connect, client_id="stackchan_mock")
    _bridge.connect()
    _bridge.loop_start()
    time.sleep(1)  # let the connection settle

    if action == "button":
        _bridge.publish("sensor/button", {"button": "A", "event": "press"})
    elif action == "touch":
        _bridge.publish("sensor/touch", {"value": True})
    elif action == "shake":
        _bridge.publish("sensor/imu", {"event": "shake"})
    elif action == "vision":
        # Face detected, off-center to the right and slightly up.
        _bridge.publish("sensor/vision", {"face_detected": True, "face_x": 0.8, "face_y": 0.3})
    elif action == "vision_lost":
        for _ in range(6):
            _bridge.publish("sensor/vision", {"face_detected": False})
    elif action == "listen":
        print(f"🤖 [MockDevice] 監聽中 (broker={config.MQTT_HOST}:{config.MQTT_PORT})... Ctrl+C 結束")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

    time.sleep(1)  # give pub/sub time to flush
    _bridge.loop_stop()


if __name__ == "__main__":
    main()
