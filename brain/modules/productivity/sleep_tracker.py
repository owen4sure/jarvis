"""Local sleep log for the Stack-chan "睡眠守門人" wish-list item.

A full implementation would read from a phone/wearable sleep API, but no
such data source exists in this project. This module provides the local
MVP (same pattern as `mood_tracker.py`/`expense_tracker.py`): the user
logs bedtime/wake time themselves via `/sleep start` and `/sleep end`,
and `bedtime_check()` (a scheduled skill, see `daily_content_skills.py`)
nags the user if they haven't gone to bed by their target bedtime.

Storage: `config/sleep.json`:
    {
        "target_bedtime": "23:00",
        "logs": [{"date": "2026-06-15", "bedtime": "23:30", "wake_time": "07:10"}]
    }
"""

import json
import os
from datetime import datetime

CONFIG_PATH = "/Users/chenyouwei/Hermes_Brain/config/sleep.json"

DEFAULT_TARGET_BEDTIME = "23:00"


def _load():
    if not os.path.exists(CONFIG_PATH):
        return {"target_bedtime": DEFAULT_TARGET_BEDTIME, "logs": []}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("target_bedtime", DEFAULT_TARGET_BEDTIME)
    data.setdefault("logs", [])
    return data


def _save(data):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _today_log(data):
    today = datetime.now().strftime("%Y-%m-%d")
    for log in data["logs"]:
        if log["date"] == today:
            return log
    log = {"date": today, "bedtime": None, "wake_time": None}
    data["logs"].append(log)
    return log


def start_sleep():
    data = _load()
    log = _today_log(data)
    log["bedtime"] = datetime.now().strftime("%H:%M")
    _save(data)
    return f"😴 已記錄今天的就寢時間：{log['bedtime']}"


def end_sleep():
    data = _load()
    log = _today_log(data)
    log["wake_time"] = datetime.now().strftime("%H:%M")
    _save(data)
    return f"☀️ 已記錄今天的起床時間：{log['wake_time']}"


def status():
    data = _load()
    if not data["logs"]:
        return f"目前沒有任何睡眠紀錄。目標就寢時間：{data['target_bedtime']}"
    log = data["logs"][-1]
    bedtime = log.get("bedtime") or "未記錄"
    wake_time = log.get("wake_time") or "未記錄"
    return (
        f"📋 最近一筆睡眠紀錄（{log['date']}）\n"
        f"就寢：{bedtime}\n起床：{wake_time}\n"
        f"目標就寢時間：{data['target_bedtime']}"
    )


def set_target_bedtime(hhmm):
    data = _load()
    data["target_bedtime"] = hhmm
    _save(data)
    return f"✅ 目標就寢時間已設為 {hhmm}"


def bedtime_check():
    """Returns a reminder string if it's past the target bedtime and the
    user hasn't logged `/sleep start` yet today, else None."""
    data = _load()
    now = datetime.now()
    target = data["target_bedtime"]
    if now.strftime("%H:%M") < target:
        return None

    today = now.strftime("%Y-%m-%d")
    for log in data["logs"]:
        if log["date"] == today and log.get("bedtime"):
            return None

    return f"😴 已經過了目標就寢時間（{target}）了，該睡了！記錄請用 /sleep start"
