"""Free, local TTS using macOS's built-in `say` command.

Avoids any paid TTS API - keeps the whole voice loop inside the
$5/month budget. Output is a 16kHz mono WAV file, small enough for
StackChan to stream/play over HTTP.
"""

import os
import subprocess
import uuid

from . import config


def synthesize(text: str) -> str:
    """Render `text` to a WAV file in AUDIO_OUTBOX_DIR, return its path."""
    os.makedirs(config.AUDIO_OUTBOX_DIR, exist_ok=True)

    file_id = uuid.uuid4().hex
    aiff_path = os.path.join(config.AUDIO_OUTBOX_DIR, f"{file_id}.aiff")
    wav_path = os.path.join(config.AUDIO_OUTBOX_DIR, f"{file_id}.wav")

    subprocess.run(
        ["say", "-v", config.TTS_VOICE, "-r", str(config.TTS_RATE), "-o", aiff_path, text],
        check=True,
    )
    subprocess.run(
        ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", aiff_path, wav_path],
        check=True,
    )
    os.remove(aiff_path)

    return wav_path
