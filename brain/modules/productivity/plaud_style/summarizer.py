"""Real meeting summarization via Gemini.

Replaces the old hardcoded "Project Phoenix" summary, which was returned
for every transcript regardless of content.
"""

from modules.embodied.gemini_client import GeminiClient

_FALLBACK = {
    "summary": "（摘要產生失敗，請參考下方逐字稿）",
    "topics": [],
    "sentiment": "未知",
    "atmosphere": "未知",
}

_PROMPT = (
    "你是會議記錄助理。閱讀以下逐字稿，輸出一個 JSON 物件，格式為："
    '{"summary": "一段繁體中文摘要", "topics": ["重點主題1", "重點主題2"], '
    '"sentiment": "整體情緒的簡短形容", "atmosphere": "會議氛圍的描述"}'
    "只輸出 JSON 物件本身，不要加任何說明文字或 markdown 標記。\n\n逐字稿：\n"
)


class Summarizer:
    def __init__(self):
        self.gemini = GeminiClient()

    def summarize(self, transcript):
        print("🧠 [Summarizer] 正在分析會議語義與情緒...")
        transcript_text = "\n".join(
            f"[{seg.get('timestamp', '')}] {seg.get('speaker', '')}: {seg.get('text', '')}"
            for seg in transcript
        )
        return self.gemini.generate_json(_PROMPT + transcript_text, fallback=_FALLBACK)
