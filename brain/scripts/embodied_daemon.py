"""Hermes Embodied Daemon - the always-on bridge between Hermes (Mac)
and StackChan (ESP32).

Run this once StackChan is connected to the same WiFi/MQTT broker:

    ./.venv/bin/python -m scripts.embodied_daemon

It wires together:
- mqtt_bridge   : MQTT transport (cmd/* out, sensor/*+status/* in)
- offline_sync  : requests + ingests StackChan's offline event buffer
- sensory_listener : logs sensor events + dispatches skill reactions
- skills/*      : pluggable reactions (see modules/embodied/skills/__init__.py)
- audio_bridge  : FastAPI server for the voice loop (HTTP, runs in a thread)
"""

import threading

import uvicorn

from modules.embodied import config, offline_sync, audio_bridge
from modules.embodied.mqtt_bridge import MQTTBridge
from modules.embodied.sensory_listener import SensoryListener
from modules.embodied.skill_context import SkillContext
from modules.embodied.skills import load_skills


def _on_connect(bridge):
    offline_sync.request_sync(bridge)


def _run_audio_bridge(bridge):
    audio_bridge.app.state.bridge = bridge
    uvicorn.run(
        audio_bridge.app,
        host=config.AUDIO_BRIDGE_HOST,
        port=config.AUDIO_BRIDGE_PORT,
        log_level="warning",
    )


def main():
    print("🌌 [Hermes Embodied Daemon] 啟動中...")
    print(f"    Hermes 連線 broker : {config.MQTT_HOST}:{config.MQTT_PORT}")
    print("    ── StackChan 韌體請設定以下位址（本機 LAN IP）──")
    print(f"    MQTT broker : {config.LAN_HOST}:{config.MQTT_PORT}")
    print(f"    語音上傳   : http://{config.LAN_HOST}:{config.AUDIO_BRIDGE_PORT}/voice")
    print(f"    Topic 前綴 : {config.TOPIC_PREFIX}")

    bridge = MQTTBridge(on_connect=_on_connect)

    offline_sync.register(bridge)
    sensory = SensoryListener(bridge)

    ctx = SkillContext(bridge, sensory)
    loaded = load_skills(ctx)
    print(f"    Skills      : {', '.join(loaded)}")

    audio_thread = threading.Thread(target=_run_audio_bridge, args=(bridge,), daemon=True)
    audio_thread.start()

    bridge.connect()
    bridge.loop_forever()


if __name__ == "__main__":
    main()
