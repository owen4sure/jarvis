"""Shared context object passed to every embodied skill.

Skills should only need this object plus the standard library -
they should not import mqtt_bridge/config directly. This keeps the
wiring in one place (scripts/embodied_daemon.py) and makes skills
easy to test/replace independently.
"""

import os

from . import config, tts
from .command_mapper import send_command


class SkillContext:
    def __init__(self, bridge, sensory):
        self.bridge = bridge
        self.sensory = sensory

    def send_command(self, intent, **kwargs):
        return send_command(self.bridge, intent, **kwargs)

    def speak(self, text: str) -> str:
        """Synthesize `text` to speech and tell StackChan to play it. Returns the audio URL.

        Uses LAN_HOST (the Mac's LAN IP), not MQTT_HOST (127.0.0.1) — StackChan
        fetches this URL over the network, so it must be reachable from the robot.
        """
        wav_path = tts.synthesize(text)
        filename = os.path.basename(wav_path)
        url = f"http://{config.LAN_HOST}:{config.AUDIO_BRIDGE_PORT}/audio/{filename}"
        self.send_command("SPEAK", url=url)
        return url
