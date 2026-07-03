"""Central config loader for the embodied (StackChan) integration.

All modules in `modules/embodied/` read settings from
`config/embodied.json` through this module so there is a single
place to change the MQTT broker address, topic prefix, ports, etc.
"""

import json
import os

BASE_DIR = "/Users/USERNAME/Hermes_Brain"
CONFIG_PATH = os.path.join(BASE_DIR, "config", "embodied.json")


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


_config = load_config()

MQTT_HOST = _config["mqtt"]["host"]
# LAN IP of this Mac — what StackChan firmware should point its MQTT + audio
# URL at. MQTT_HOST (above) is what Hermes itself uses to reach the local
# broker (127.0.0.1, bulletproof). They differ on purpose: Hermes connects
# locally, StackChan connects over the LAN. Update lan_host if the Mac's IP
# changes (run `ipconfig getifaddr en0`).
LAN_HOST = _config["mqtt"].get("lan_host", _config["mqtt"]["host"])
MQTT_PORT = _config["mqtt"]["port"]
MQTT_CLIENT_ID = _config["mqtt"]["client_id"]
TOPIC_PREFIX = _config["mqtt"]["topic_prefix"]

AUDIO_BRIDGE_HOST = _config["audio_bridge"]["host"]
AUDIO_BRIDGE_PORT = _config["audio_bridge"]["port"]

TTS_VOICE = _config["tts"]["voice"]
TTS_RATE = _config["tts"]["rate"]

def get_gemini_model():
    """Re-read config/embodied.json so /model switches take effect
    immediately, without restarting the bot/daemon.

    This is the user-switchable "chat" model (Telegram replies,
    StackChan voice replies, /research). Defaults to a text-only model
    (e.g. gemma), so it must NOT be used for audio input."""
    return load_config()["gemini"]["model"]


def set_gemini_model(model_id):
    """Persist a new Gemini model id to config/embodied.json."""
    cfg = load_config()
    cfg["gemini"]["model"] = model_id
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)


def get_transcribe_model():
    """Audio-capable model used to transcribe StackChan voice input.
    Kept separate from get_gemini_model() because the user-switchable
    chat model may not support audio (e.g. gemma)."""
    return load_config()["gemini"]["transcribe_model"]


ENABLED_SKILLS = _config["skills"]["enabled"]

LOCATION = _config["location"]

# Shared paths
EVENTS_LOG_PATH = os.path.join(BASE_DIR, "memory", "logs", "embodied_events.jsonl")
AUDIO_INBOX_DIR = os.path.join(BASE_DIR, "memory", "audio_inbox")
AUDIO_OUTBOX_DIR = os.path.join(BASE_DIR, "memory", "audio_outbox")


def topic(suffix):
    """Build a full MQTT topic from the configured prefix, e.g. topic('cmd/expression')."""
    return f"{TOPIC_PREFIX}/{suffix}"
