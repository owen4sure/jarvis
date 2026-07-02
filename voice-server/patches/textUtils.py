import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
EMOJI_MAP = {
    "😂": "funny",
    "😭": "crying",
    "😠": "angry",
    "😔": "sad",
    "😍": "loving",
    "😲": "surprised",
    "😱": "shocked",
    "🤔": "thinking",
    "😌": "relaxed",
    "😴": "sleepy",
    "😜": "silly",
    "🙄": "confused",
    "😶": "neutral",
    "🙂": "happy",
    "😆": "laughing",
    "😳": "embarrassed",
    "😉": "winking",
    "😎": "cool",
    "🤤": "delicious",
    "😘": "kissy",
    "😏": "confident",
}
EMOJI_RANGES = [
    (0x1F600, 0x1F64F),
    (0x1F300, 0x1F5FF),
    (0x1F680, 0x1F6FF),
    (0x1F900, 0x1F9FF),
    (0x1FA70, 0x1FAFF),
    (0x2600, 0x26FF),
    (0x2700, 0x27BF),
]


def get_string_no_punctuation_or_emoji(s):
    """去除字符串首尾的空格、标点符号和表情符号"""
    chars = list(s)
    # 处理开头的字符
    start = 0
    while start < len(chars) and is_punctuation_or_emoji(chars[start]):
        start += 1
    # 处理结尾的字符
    end = len(chars) - 1
    while end >= start and is_punctuation_or_emoji(chars[end]):
        end -= 1
    return "".join(chars[start : end + 1])


def is_punctuation_or_emoji(char):
    """检查字符是否为空格、指定标点或表情符号"""
    # 定义需要去除的中英文标点（包括全角/半角）
    punctuation_set = {
        "，",
        ",",  # 中文逗号 + 英文逗号
        "。",
        ".",  # 中文句号 + 英文句号
        "！",
        "!",  # 中文感叹号 + 英文感叹号
        "“",
        "”",
        '"',  # 中文双引号 + 英文引号
        "：",
        ":",  # 中文冒号 + 英文冒号
        "-",
        "－",  # 英文连字符 + 中文全角横线
        "、",  # 中文顿号
        "[",
        "]",  # 方括号
        "【",
        "】",  # 中文方括号
    }
    if char.isspace() or char in punctuation_set:
        return True
    return is_emoji(char)


# Hermes: 沒有 emoji 時，從回覆內容的關鍵字判斷情緒（讓表情豐富、貼合反應）
_EMOTION_KEYWORDS = [
    ("surprised", "😲", ["哇", "天啊", "天哪", "竟然", "居然", "真的假的", "不會吧", "嚇", "驚", "誇張", "不敢相信", "什麼！", "什麼?"]),
    ("sad", "😔", ["難過", "辛苦", "抱歉", "對不起", "可惜", "遺憾", "心疼", "別難過", "唉", "嗚", "難受", "委屈", "節哀", "傷心"]),
    ("thinking", "🤔", ["讓我想", "我想想", "這個嘛", "嗯…", "或許", "可能是", "有點難", "思考", "想一下", "不確定", "我猜"]),
    ("embarrassed", "😳", ["不好意思", "害羞", "糗", "尷尬", "歹勢", "臉紅"]),
    ("laughing", "😆", ["哈哈", "太好笑", "笑死", "太搞笑", "哈哈哈"]),
    ("happy", "😆", ["太棒", "恭喜", "好耶", "讚啦", "太好了", "好開心", "厲害", "太強", "棒棒", "好玩", "喜歡你", "愛你", "好幸福", "讚"]),
]


def _detect_emotion_by_keyword(text):
    for emotion, emoji, kws in _EMOTION_KEYWORDS:
        for kw in kws:
            if kw in text:
                return emoji, emotion
    return None


async def get_emotion(conn: "ConnectionHandler", text):
    """获取文本内的情绪消息"""
    emoji = "🙂"
    emotion = "happy"
    found_emoji = False
    for char in text:
        if char in EMOJI_MAP:
            emoji = char
            emotion = EMOJI_MAP[char]
            found_emoji = True
            break
    # 沒有 emoji 就用關鍵字判斷，讓表情貼合內容（否則一律 happy）
    if not found_emoji:
        kw = _detect_emotion_by_keyword(text)
        if kw is not None:
            emoji, emotion = kw
        else:
            emoji, emotion = "🙂", "happy"
    try:
        await conn.websocket.send(
            json.dumps(
                {
                    "type": "llm",
                    "text": emoji,
                    "emotion": emotion,
                    "session_id": conn.session_id,
                }
            )
        )
    except Exception as e:
        conn.logger.bind(tag=TAG).warning(f"发送情绪表情失败，错误:{e}")
    return


def is_emoji(char):
    """检查字符是否为emoji表情"""
    code_point = ord(char)
    return any(start <= code_point <= end for start, end in EMOJI_RANGES)


def check_emoji(text):
    """去除文本中的所有emoji表情"""
    return "".join(char for char in text if not is_emoji(char) and char != "\n")
