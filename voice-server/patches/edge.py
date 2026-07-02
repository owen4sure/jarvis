"""EdgeTTS provider — Hermes 加上「時間感知語氣」(Goal 2 的靈魂)。
深夜自動放慢語速、降低音調，講話像氣音安慰；白天正常活潑。
"""
import os
import re
import uuid
from datetime import datetime

import edge_tts
import zoneinfo

from core.providers.tts.base import TTSProviderBase

_TZ = zoneinfo.ZoneInfo("Asia/Taipei")

# emoji 與各種雜符號的範圍（表情/符號/旗幟/補充符號等），唸出來很糟或被唸成符號名 → 進 TTS 前清掉。
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"   # 各式 emoji（表情、物件、符號、補充）
    "\U00002600-\U000027BF"   # 雜項符號 + dingbats（☀☔✨✅❤ 等）
    "\U0001F000-\U0001F0FF"   # 麻將/牌
    "\U00002190-\U000021FF"   # 箭頭
    "\U0000FE00-\U0000FE0F"   # 變體選擇器
    "\U00002B00-\U00002BFF"   # 雜項符號與箭頭（⭐ 等）
    "]+", flags=re.UNICODE)


def _sanitize_for_speech(text):
    """進 EdgeTTS 前清掉 emoji 和殘留 markdown 符號，讓唸出來乾淨（最後防線，不靠模型自律）。"""
    if not text:
        return text
    s = _EMOJI_RE.sub("", text)
    # 殘留的 markdown 強調/標題/條列符號（**粗體** ## 標題 - 項目）→ 去符號留文字
    s = s.replace("**", "").replace("##", "").replace("#", "")
    s = re.sub(r"(?m)^\s*[\*\-•]\s+", "", s)   # 行首的項目符號
    s = s.replace("*", "")                       # 任何殘留星號（粗體/斜體符號）
    s = re.sub(r"[ \t]{2,}", " ", s)            # 多餘空白收斂
    return s.strip()


def _rate_pitch_by_time():
    """依台灣時間回傳 (rate, pitch)。
    ★預設關閉變調★：pitch shift 會讓 EdgeTTS 人聲變粗糙（聽起來像雜訊），
    預設一律乾淨原聲 (+0%/+0Hz)。要恢復「時段語氣」設 HERMES_TTS_PROSODY=1，
    且即使開啟也只微調語速、絕不改 pitch（pitch shift 是失真主因）。
    """
    if os.getenv("HERMES_TTS_PROSODY", "0") != "1":
        return "+0%", "+0Hz"   # 乾淨原聲（預設）
    h = datetime.now(_TZ).hour
    if h >= 23 or h < 6:        # 深夜：只放慢一點，不改 pitch
        return "-8%", "+0Hz"
    if 6 <= h < 9:             # 清晨：略慢
        return "-4%", "+0Hz"
    return "+0%", "+0Hz"       # 其他：正常


class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        if config.get("private_voice"):
            self.voice = config.get("private_voice")
        else:
            self.voice = config.get("voice")
        self.audio_file_type = config.get("format", "mp3")

    def generate_filename(self, extension=".mp3"):
        return os.path.join(
            self.output_file,
            f"tts-{datetime.now().date()}@{uuid.uuid4().hex}{extension}",
        )

    async def text_to_speak(self, text, output_file):
        # EdgeTTS（微軟免費服務）偶爾會「No audio received」(被限流/瞬斷)→ 重試最多3次，
        # 避免使用者偶發「它沒回我」（其實是 TTS 那一下沒出聲）。
        # 【最後防線】清掉 emoji 和殘留 markdown 符號:模型有時會違反人格的語音鐵律(夾😆😲、星號**)，
        # 這些唸出來很糟或被唸成「星號」。不管模型輸出什麼,進 TTS 前一律洗乾淨。
        text = _sanitize_for_speech(text)
        import asyncio as _a
        last_err = None
        for attempt in range(3):
            try:
                rate, pitch = _rate_pitch_by_time()
                communicate = edge_tts.Communicate(
                    text, voice=self.voice, rate=rate, pitch=pitch
                )
                if output_file:
                    os.makedirs(os.path.dirname(output_file), exist_ok=True)
                    got = False
                    with open(output_file, "wb") as f:
                        async for chunk in communicate.stream():
                            if chunk["type"] == "audio":
                                f.write(chunk["data"])
                                got = True
                    if got:
                        return
                    last_err = "empty audio"
                else:
                    audio_bytes = b""
                    async for chunk in communicate.stream():
                        if chunk["type"] == "audio":
                            audio_bytes += chunk["data"]
                    if audio_bytes:
                        return audio_bytes
                    last_err = "empty audio"
            except Exception as e:
                last_err = e
            if attempt < 2:
                await _a.sleep(0.4 * (attempt + 1))  # 退避後重試
        from config.logger import setup_logging
        setup_logging().bind(tag="EdgeTTS").error(f"Edge TTS 重試3次仍失敗: {last_err}")
        return None
