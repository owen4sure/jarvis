"""
macsay TTS engine — 用 macOS 內建 `say`（中文聲音 Meijia）合成語音。
給 stackchan-mcp gateway 用：零安裝、繁中自然、免架伺服器。
產出 16kHz mono signed-16-bit-LE PCM，orchestrator 再轉 Opus 推到裝置。

安裝：複製到 stackchan_mcp/tts/ 並在 __init__.py 註冊（見 scripts/install_mac_tts.sh）。
"""
from __future__ import annotations

import asyncio
import os
import struct
import tempfile

from .base import TTSEngine, get_registry

DEFAULT_VOICE = os.environ.get("MACSAY_VOICE", "Meijia")  # 繁中；可改 Eddy/Flo/Mei-Jia 等


def _pcm_from_wav(data: bytes) -> bytes:
    """從 WAV bytes 取出 PCM（找 data chunk）。"""
    idx = data.find(b"data")
    if idx < 0 or idx + 8 > len(data):
        return b""
    size = struct.unpack("<I", data[idx + 4:idx + 8])[0]
    start = idx + 8
    return data[start:start + size] if size else data[start:]


class MacSayEngine(TTSEngine):
    name = "macsay"
    supports_emoji_style = False

    async def synthesize(self, text: str, **opts) -> bytes:
        text = (text or "").strip()
        if not text:
            raise ValueError("macsay: empty text")
        voice = opts.get("voice_name") or DEFAULT_VOICE
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            wav_path = f.name
        try:
            proc = await asyncio.create_subprocess_exec(
                "say", "-v", voice,
                "--data-format=LEI16@16000", "--file-format=WAVE",
                "-o", wav_path, text,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            _, err = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"macsay 'say' failed: {err.decode('utf-8','ignore')[:200]}")
            with open(wav_path, "rb") as fh:
                wav = fh.read()
            pcm = _pcm_from_wav(wav)
            if not pcm:
                raise RuntimeError("macsay produced no PCM")
            return pcm
        finally:
            try:
                os.remove(wav_path)
            except Exception:
                pass


def register() -> None:
    get_registry().register(MacSayEngine())
