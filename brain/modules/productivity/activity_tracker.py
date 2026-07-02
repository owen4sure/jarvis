"""Tracks the last time the user sent *anything* to the Telegram bot.

This is the presence signal used by `emergency_contact.late_night_check()`
for the "深夜未回通知緊急聯絡人" wish-list item: if the user hasn't sent a
single message all day by a cutoff time, we don't know whether they're
just asleep early or something's wrong, so we let a designated emergency
contact know.

Storage: `config/activity.json`: {"last_activity": "2026-06-15T22:13:04"}
"""

import json
import os
from datetime import datetime

CONFIG_PATH = "/Users/chenyouwei/Hermes_Brain/config/activity.json"


def record_activity():
    data = {"last_activity": datetime.now().isoformat()}
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def last_activity_date():
    """Returns the date (YYYY-MM-DD) of the last recorded activity, or
    None if nothing has ever been recorded."""
    if not os.path.exists(CONFIG_PATH):
        return None
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    last = data.get("last_activity")
    if not last:
        return None
    return datetime.fromisoformat(last).strftime("%Y-%m-%d")
