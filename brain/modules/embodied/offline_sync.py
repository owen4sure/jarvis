"""Temporal Loop: pull StackChan's offline event buffer on (re)connect.

Implements specs/phase_04_embodied_soul/auto_sync_spec.md:
1. On MQTT connect, publish to `sync/request`.
2. StackChan replies on `sync/buffer` with each buffered event it
   logged to LittleFS while Hermes/Mac was offline.
3. We log each one into memory/logs/embodied_events.jsonl, tagged
   with source="offline_buffer" and the device's own timestamp, so
   Hermes can later say "我剛剛在離線時感覺到...".
"""

import json
import os
from datetime import datetime

from . import config
from .command_mapper import send_command


def register(bridge):
    """Wire up sync/buffer handling and request a sync on every connect."""
    bridge.on("sync/buffer", _handle_buffer_entry)


def request_sync(bridge):
    send_command(bridge, "SYNC_REQUEST")


def _handle_buffer_entry(payload, full_topic):
    os.makedirs(os.path.dirname(config.EVENTS_LOG_PATH), exist_ok=True)
    record = {
        "timestamp": datetime.now().isoformat(),
        "device_timestamp": payload.get("timestamp"),
        "topic": "sync/buffer",
        "source": "offline_buffer",
        "payload": payload,
    }
    with open(config.EVENTS_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"🕰️ [OfflineSync] 補登離線事件: {payload}")
