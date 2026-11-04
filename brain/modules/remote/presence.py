"""
presence — 在線心跳 + Telegram offset 持久化 + 離線時長偵測
============================================================================
解決「Mac 關機重開」：
  - offset 寫到檔案 → 重開機後從上次位置接續，不漏訊息也不重複處理。
  - 心跳時間戳 → 重開機時比對「上次活著」到「現在」，算出離線多久，
    可主動告訴使用者「我離線了 X，現在回來了」。
"""
import json
import os
import time

_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "memory", "heartbeat.json",
)


def write(offset=None) -> None:
    data = {"ts": time.time()}
    if offset is not None:
        data["offset"] = offset
    else:
        prev = read()
        if prev and "offset" in prev:
            data["offset"] = prev["offset"]
    try:
        os.makedirs(os.path.dirname(_PATH), exist_ok=True)
        tmp = _PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, _PATH)
    except Exception:
        pass


def read() -> dict:
    try:
        with open(_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def last_offset():
    return read().get("offset")


def downtime_seconds() -> float:
    """距離上次心跳過了多久（秒）。沒有紀錄回 0。"""
    prev = read()
    if not prev or "ts" not in prev:
        return 0.0
    return max(0.0, time.time() - prev["ts"])


def human_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分鐘"
    if seconds < 86400:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h} 小時{m} 分" if m else f"{h} 小時"
    return f"{seconds // 86400} 天"
