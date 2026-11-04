"""Real speech-to-text + speaker diarization via Gemini's audio understanding.

Replaces the old simulation engine (which always returned the same
hardcoded "Project Phoenix" transcript regardless of input audio).
"""

import mimetypes

from modules.embodied.gemini_client import GeminiClient


class STTEngine:
    def __init__(self):
        self.gemini = GeminiClient()

    def transcribe(self, audio_path):
        """Transcribe `audio_path` into a diarized, timestamped transcript:
        a list of {"timestamp": "mm:ss", "speaker": str, "text": str}."""
        mime_type, _ = mimetypes.guess_type(audio_path)
        mime_type = mime_type or "audio/wav"
        with open(audio_path, "rb") as f:
            audio_bytes = f.read()
        print(f"🎙️ [STT] Transcribing: {audio_path} ({mime_type})")
        return self.gemini.transcribe_diarized(audio_bytes, mime_type=mime_type)
