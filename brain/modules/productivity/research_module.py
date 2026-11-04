"""Deep research via Gemini's grounded Google Search tool.

Free (within the existing Gemini key pool, no extra API/credentials),
and avoids scraping search engines directly (which tends to hit
anti-bot challenges).
"""

import time

from google import genai
from google.genai import types

from scripts.key_manager import KeyManager
from modules.embodied import config as embodied_config

# 只有「這個 key 真的有問題」的錯誤碼才標記 key 進入冷卻：
# 429=配額/速率限制, 401/403=key無效或無權限。
# 500/503 等是 Gemini 端的暫時性錯誤，跟 key 本身無關，短暫等待後直接重試即可。
_KEY_LEVEL_ERROR_CODES = {401, 403, 429}

RESEARCH_PROMPT = (
    "你是 Hermes 的研究助理。請針對以下問題做簡短但有根據的研究，"
    "用繁體中文回答，條列重點，並在最後列出參考網址。"
)


class ResearchModule:
    def __init__(self):
        self.key_manager = KeyManager()

    def deep_search(self, query: str) -> str:
        """Run a grounded web search + summary for `query`. Rotates through
        the key pool on key-level errors, and briefly retries in place on
        transient server errors (500/503), same as GeminiClient."""
        num_keys = len(self.key_manager.config.get("api_keys", [])) or 1
        max_attempts = num_keys + 2
        last_err = None
        for _ in range(max_attempts):
            _kidx, _kkey = self.key_manager.get_key_with_index()
            client = genai.Client(api_key=_kkey)
            try:
                response = client.models.generate_content(
                    model=embodied_config.get_gemini_model(),
                    contents=[RESEARCH_PROMPT, query],
                    config=types.GenerateContentConfig(
                        tools=[types.Tool(google_search=types.GoogleSearch())]
                    ),
                )
                return (getattr(response, "text", None) or "").strip()
            except Exception as e:
                last_err = e
                code = getattr(e, "code", 500)
                if code in _KEY_LEVEL_ERROR_CODES:
                    self.key_manager.report_error(code, _kidx)
                else:
                    time.sleep(1)
        raise last_err
