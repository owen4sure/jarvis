"""
pending_queue — 大腦暫時連不上時，把使用者的問題存起來，恢復後自動補回覆
============================================================================
Gemini 全掛 / Mac 剛開機還沒連上時，Telegram 來的訊息不會被丟掉：先排進
`memory/pending_replies.json`，並回一句友善訊息。等大腦恢復，retrier 會把它們
拿出來重新產生回覆並補送，然後從佇列移除。

純檔案 JSON + 原子寫入；跨重開機也有效（Mac 關機重開後佇列還在）。
"""
import json
import os
import time
import uuid

_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "memory", "pending_replies.json",
)
MAX_AGE_SECONDS = 24 * 3600  # 超過一天的就不補了（太舊補回覆很怪）


def _load() -> list:
    try:
        with open(_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save(items: list) -> None:
    os.makedirs(os.path.dirname(_PATH), exist_ok=True)
    tmp = _PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, _PATH)


def enqueue(chat_id, text: str) -> None:
    items = _load()
    items.append({
        "id": uuid.uuid4().hex,
        "chat_id": chat_id,
        "text": text,
        "ts": time.time(),
        "tries": 0,
    })
    _save(items)


def list_pending() -> list:
    return _load()


def remove(entry_id: str) -> None:
    _save([it for it in _load() if it.get("id") != entry_id])


def bump_try(entry_id: str) -> None:
    items = _load()
    for it in items:
        if it.get("id") == entry_id:
            it["tries"] = it.get("tries", 0) + 1
    _save(items)


def purge_expired() -> int:
    now = time.time()
    items = _load()
    keep = [it for it in items if now - it.get("ts", now) < MAX_AGE_SECONDS]
    removed = len(items) - len(keep)
    if removed:
        _save(keep)
    return removed
