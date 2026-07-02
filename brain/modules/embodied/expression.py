"""
expression — 情緒表達引擎：讓 StackChan 講話時臉 + 燈 + 動作配合情緒
============================================================================
把一段回覆文字 → 推測情緒 → 設定對應的「表情 + LED 顏色 + 小動作」再說出來。
讓它從「會講話的喇叭」變成「有表情、有反應的夥伴」。

表情用 gateway 實際支援的 enum：idle/happy/thinking/sad/surprised/embarrassed/off
"""
import re

# 情緒 → (avatar 表情, LED 顏色 RGB, 點頭/搖頭可選)
EMO_MAP = {
    "happy":      ("happy",       (0, 200, 80)),    # 綠
    "excited":    ("happy",       (255, 140, 0)),   # 橙（興奮）
    "sad":        ("sad",         (0, 60, 160)),    # 暗藍
    "thinking":   ("thinking",    (0, 160, 160)),   # 青
    "surprised":  ("surprised",   (255, 210, 0)),   # 黃
    "embarrassed":("embarrassed", (255, 90, 140)),  # 粉
    "angry":      ("surprised",   (200, 0, 0)),     # 無 angry 臉 → 驚訝臉 + 紅燈
    "love":       ("happy",       (255, 60, 120)),  # 粉紅
    "neutral":    ("idle",        (60, 60, 70)),    # 微光
}

# 關鍵字 / emoji 啟發式（零額外 API 成本）
_RULES = [
    ("love",       ["愛你", "想你", "抱抱", "❤", "😍", "🥰", "麼麼", "親親"]),
    ("excited",    ["太棒", "讚啦", "衝", "出發", "贏了", "好耶", "🎉", "🔥", "yes", "讚！"]),
    ("happy",      ["哈哈", "嘻嘻", "開心", "謝謝", "好喔", "沒問題", "😄", "😊", "👍", "讚"]),
    ("sad",        ["難過", "抱歉", "對不起", "唉", "可惜", "失敗", "😢", "😞", "💔"]),
    ("surprised",  ["哇", "天啊", "真的假的", "什麼!", "竟然", "居然", "😲", "😮", "?!", "！？"]),
    ("embarrassed",["不好意思", "害羞", "糗", "尷尬", "😅", "🙈"]),
    ("angry",      ["生氣", "可惡", "討厭", "煩", "😠", "😡"]),
    ("thinking",   ["讓我想", "嗯…", "可能", "也許", "我查", "思考", "🤔"]),
]


def detect_emotion(text: str) -> str:
    if not text:
        return "neutral"
    low = text.lower()
    for emo, kws in _RULES:
        if any(k.lower() in low for k in kws):
            return emo
    # 問號多 → thinking；驚嘆多 → excited
    if text.count("？") + text.count("?") >= 1 and len(text) < 40:
        return "thinking"
    if text.count("！") + text.count("!") >= 2:
        return "excited"
    return "neutral"


def express(robot, emotion: str) -> None:
    """只設定表情 + LED（不說話）。best-effort，裝置不在也不報錯。"""
    face, (r, g, b) = EMO_MAP.get(emotion, EMO_MAP["neutral"])
    try:
        robot.set_avatar(face)
    except Exception:
        pass
    try:
        robot.set_all_leds(r, g, b)
    except Exception:
        pass


def expressive_say(robot, text: str, emotion: str = None) -> dict:
    """配合情緒：設表情+燈 → 說出來。回傳 say 的結果。"""
    emo = emotion or detect_emotion(text)
    express(robot, emo)
    try:
        return robot.say(text)
    except Exception as e:
        return {"ok": False, "error": repr(e)}
