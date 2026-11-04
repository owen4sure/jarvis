"""Gemini ASR — 用 Gemini 多模態轉錄語音，完美支援中英文混雜（code-switching）。
本地 SenseVoice 對「同一句中英混」會整句判成一種語言而丟字；Gemini 直接照實轉錄。
音訊經金鑰輪換代理(:8808)送到 Gemini 原生 :generateContent 端點。
"""
import base64
import os
import time
from typing import List, Optional, Tuple

import requests

from core.providers.asr.base import ASRProviderBase
from core.providers.asr.dto.dto import InterfaceType
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

_PROMPT = (
    "請逐字轉錄這段語音的內容。原本講中文就用繁體中文、講英文就保留英文原樣，"
    "中英文混在一起時照實保留兩者（例如「幫我打開 YouTube」就輸出「幫我打開 YouTube」）。"
    "只輸出轉錄的文字本身，不要加任何說明、引號或標籤。若完全沒有人聲就輸出空字串。"
)


class ASRProvider(ASRProviderBase):
    def __init__(self, config: dict, delete_audio_file: bool):
        super().__init__()
        self.interface_type = InterfaceType.NON_STREAM
        self.base_url = config.get(
            "base_url", "http://host.docker.internal:8808/v1beta"
        ).rstrip("/")
        self.model = config.get("model_name", "gemini-2.5-flash")
        self.output_dir = config.get("output_dir", "tmp/")
        self.delete_audio_file = delete_audio_file
        os.makedirs(self.output_dir, exist_ok=True)

    def requires_file(self) -> bool:
        # 需要 base 類先把 PCM 合成 WAV 檔，我們讀檔轉 base64
        return True

    async def speech_to_text(
        self,
        opus_data: List[bytes],
        session_id: str,
        audio_format="opus",
        artifacts=None,
    ) -> Tuple[Optional[str], Optional[str]]:
        file_path = None
        try:
            if artifacts is None:
                return "", None
            file_path = artifacts.file_path
            if not file_path or not os.path.exists(file_path):
                return "", file_path
            with open(file_path, "rb") as f:
                wav_bytes = f.read()
            b64 = base64.b64encode(wav_bytes).decode("ascii")

            url = f"{self.base_url}/models/{self.model}:generateContent"
            payload = {
                "contents": [
                    {
                        "parts": [
                            {"text": _PROMPT},
                            {"inline_data": {"mime_type": "audio/wav", "data": b64}},
                        ]
                    }
                ],
                "generationConfig": {"temperature": 0, "maxOutputTokens": 256},
            }
            start = time.time()
            r = requests.post(url, json=payload, timeout=20)
            elapsed = time.time() - start
            if r.status_code != 200:
                raise Exception(f"Gemini ASR {r.status_code}: {r.text[:200]}")
            d = r.json()
            # candidates 可能是空 list（安全封鎖/沒聽到內容）→ [0] 會 IndexError，要先擋
            _cands = d.get("candidates") or [{}]
            _parts = (_cands[0].get("content", {}) or {}).get("parts") or [{}]
            text = (_parts[0].get("text", "") or "").strip()
            logger.bind(tag=TAG).info(
                f"Gemini ASR {elapsed:.2f}s → {text[:60]}"
            )
            return text, file_path
        except Exception as e:
            logger.bind(tag=TAG).error(f"Gemini ASR 失敗: {e}")
            return "", file_path
