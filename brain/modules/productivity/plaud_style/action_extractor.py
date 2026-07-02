"""Real task/decision extraction via Gemini.

Replaces the old hardcoded "Bob/Charlie/API spec" tasks, which were
returned for every transcript regardless of content.
"""

from modules.embodied.gemini_client import GeminiClient

_FALLBACK = {"tasks": [], "decisions": []}

_PROMPT = (
    "你是會議記錄助理。閱讀以下逐字稿，輸出一個 JSON 物件，格式為："
    '{"tasks": [{"owner": "負責人", "task": "任務內容", "deadline": "期限或TBD"}], '
    '"decisions": ["會議中做出的決策1", "決策2"]}'
    "若逐字稿中沒有明確的待辦事項或決策，對應陣列回傳空陣列 []。"
    "只輸出 JSON 物件本身，不要加任何說明文字或 markdown 標記。\n\n逐字稿：\n"
)


class ActionExtractor:
    def __init__(self):
        self.gemini = GeminiClient()

    def extract_tasks(self, transcript):
        print("📋 [ActionExtractor] 正在提取待辦事項與決策...")
        transcript_text = "\n".join(
            f"[{seg.get('timestamp', '')}] {seg.get('speaker', '')}: {seg.get('text', '')}"
            for seg in transcript
        )
        return self.gemini.generate_json(_PROMPT + transcript_text, fallback=_FALLBACK)
