"""Lightweight local mood log for the Stack-chan "每日情緒 check-in + 趨勢"
wish-list item.

`/mood <1-5> [備註]` appends an entry to `config/moods.json`.
`weekly_trend()` (used by the `mood_weekly_trend` scheduled skill, see
daily_content_skills.py) summarizes the last 7 days.
"""

import json
import os
from datetime import datetime, timedelta

CONFIG_PATH = "/Users/chenyouwei/Hermes_Brain/config/moods.json"

_SCORE_LABELS = {1: "很差", 2: "不好", 3: "普通", 4: "不錯", 5: "很棒"}


def _load():
    if not os.path.exists(CONFIG_PATH):
        return {"moods": []}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def add_mood(score, note=""):
    if score not in _SCORE_LABELS:
        raise ValueError("心情分數必須是 1-5")

    data = _load()
    data["moods"].append({
        "score": score,
        "note": note,
        "date": datetime.now().strftime("%Y-%m-%d"),
    })
    _save(data)


def list_recent(days=7):
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    return [m for m in _load()["moods"] if m["date"] >= cutoff]


def weekly_trend():
    moods = list_recent(days=7)
    if not moods:
        return "📈 過去 7 天沒有任何心情紀錄。用 /mood <1-5> [備註] 記錄今天的心情吧。"

    avg = sum(m["score"] for m in moods) / len(moods)
    lines = [f"📈 過去 7 天心情趨勢：平均 {avg:.1f} / 5"]
    for m in moods:
        label = _SCORE_LABELS[m["score"]]
        note = f"（{m['note']}）" if m["note"] else ""
        lines.append(f"- {m['date']}: {m['score']}/5 {label}{note}")
    return "\n".join(lines)
