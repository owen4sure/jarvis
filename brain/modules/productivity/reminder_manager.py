"""Generic time-based reminder store, shared by the Telegram `/remind`
command and `scripts/reminder_daemon.py`.

This is the scheduling primitive flagged in SYSTEM_STATUS.md section 9:
most of the Stack-chan "定期提醒/播報" wish-list items (health reminders,
finance reports, birthday reminders, daily learning prompts, ...) all need
"do X at time Y, on schedule Z" - this module provides that primitive so
each of those can be added as a one-line entry instead of reinventing
scheduling.

Storage: `config/reminders.json` (not a secret, but user-specific data so
kept out of git like the other config/*.json files):
    {
        "reminders": [
            {"id": 1, "time": "08:00", "message": "喝水！",
             "repeat": "daily", "skill": null, "last_fired": "2026-06-15"}
        ],
        "next_id": 2
    }

`repeat` formats:
    "daily"            - every day
    "weekly:N"         - once a week, N = 0 (Mon) .. 6 (Sun)
    "monthly:DD"       - once a month, on day-of-month DD (1-31)
    "annual:MM-DD"     - once a year, e.g. birthdays/anniversaries
    "once:YYYY-MM-DD"  - a single date (e.g. document expiry, vehicle service);
                         removed automatically after it fires

`skill` (optional): name of a content generator in
`modules/productivity/daily_content_skills.py` (e.g. "daily_word",
"daily_thought", "daily_digest", "expense_weekly_report",
"mood_weekly_trend"). When set, the daemon calls that generator to build
the message body instead of using the static `message` field.

`last_fired` is the date (YYYY-MM-DD) the reminder last fired, used to
avoid firing twice on the same day/week/month if the daemon checks more
than once per minute.
"""

import json
import os
import re
from datetime import datetime

CONFIG_PATH = "/Users/chenyouwei/Hermes_Brain/config/reminders.json"

_REPEAT_PATTERN = re.compile(
    r"^(daily|weekly:[0-6]|monthly:(0?[1-9]|[12][0-9]|3[01])|"
    r"annual:(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])|"
    r"once:\d{4}-\d{2}-\d{2})$"
)


def is_repeat_token(token):
    """True if `token` looks like a `repeat` value (daily/weekly:.../
    monthly:.../annual:.../once:...), used by the `/remind` command to
    tell a repeat token apart from the start of a free-text message."""
    return bool(_REPEAT_PATTERN.match(token))


def validate_repeat(repeat):
    """Raises ValueError if `repeat` is not a recognized schedule format."""
    if not _REPEAT_PATTERN.match(repeat):
        raise ValueError(
            f"無法識別的重複規則: {repeat!r}（支援: daily / weekly:0-6 / "
            "monthly:DD / annual:MM-DD / once:YYYY-MM-DD）"
        )


def _load():
    if not os.path.exists(CONFIG_PATH):
        return {"reminders": [], "next_id": 1}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def list_reminders():
    return _load()["reminders"]


def add_reminder(time_str, message, repeat="daily", skill=None,
                 lead_minutes=0, channel="both"):
    """`time_str` must be "HH:MM" (24h). Returns the new reminder's id.

    lead_minutes：提早幾分鐘提醒（例：事件 15:00、lead=10 → 14:50 就提醒）。
    channel：用什麼方式通知 —— "both"（Telegram+語音）/ "telegram" / "voice"。"""
    datetime.strptime(time_str, "%H:%M")  # raises ValueError if invalid
    validate_repeat(repeat)
    try:
        lead_minutes = max(0, int(lead_minutes or 0))
    except Exception:
        lead_minutes = 0
    if channel not in ("both", "telegram", "voice"):
        channel = "both"

    data = _load()
    reminder_id = data["next_id"]
    data["reminders"].append({
        "id": reminder_id,
        "time": time_str,
        "message": message,
        "repeat": repeat,
        "skill": skill,
        "lead_minutes": lead_minutes,
        "channel": channel,
        "last_fired": None,
    })
    data["next_id"] = reminder_id + 1
    _save(data)
    return reminder_id


def remove_reminder(reminder_id):
    data = _load()
    before = len(data["reminders"])
    data["reminders"] = [r for r in data["reminders"] if r["id"] != reminder_id]
    _save(data)
    return len(data["reminders"]) < before


def _is_due(r, now, today, current_period_key):
    repeat = r.get("repeat", "daily")
    last_fired = r.get("last_fired")

    if repeat == "daily":
        return last_fired != today

    if repeat.startswith("weekly:"):
        weekday = int(repeat.split(":", 1)[1])
        return now.weekday() == weekday and last_fired != current_period_key

    if repeat.startswith("monthly:"):
        day_of_month = int(repeat.split(":", 1)[1])
        return now.day == day_of_month and last_fired != current_period_key

    if repeat.startswith("annual:"):
        month_day = repeat.split(":", 1)[1]
        return now.strftime("%m-%d") == month_day and last_fired != current_period_key

    if repeat.startswith("once:"):
        target_date = repeat.split(":", 1)[1]
        return today == target_date and last_fired != today

    return False


def _fire_time(r):
    """提醒實際『發出』的時間 = 事件時間 − 提早分鐘數（回 HH:MM）。lead=0 就是事件當下。"""
    lead = int(r.get("lead_minutes") or 0)
    if lead <= 0:
        return r["time"]
    try:
        from datetime import timedelta
        t = datetime.strptime(r["time"], "%H:%M") - timedelta(minutes=lead)
        return t.strftime("%H:%M")
    except Exception:
        return r["time"]


def get_due_reminders(now=None):
    """Return reminders whose schedule matches `now` and haven't fired yet
    for their current period, marking them as fired (or removing "once"
    reminders) as a side effect."""
    now = now or datetime.now()
    current_time = now.strftime("%H:%M")
    today = now.strftime("%Y-%m-%d")

    def _to_min(hhmm):
        try:
            h, m = str(hhmm).split(":")
            return int(h) * 60 + int(m)
        except Exception:
            return -1

    data = _load()
    due = []
    changed = False
    remaining = []
    cur = _to_min(current_time)
    for r in data["reminders"]:
        rep = r.get("repeat", "daily")
        # 過期的一次性提醒(日期已過)→ 直接清掉,不要卡在清單變成永遠的「過期未響」
        if rep.startswith("once:"):
            d = rep[5:]
            if d and d < today:
                changed = True
                continue  # 丟掉,不加回 remaining

        # 用「發出時間」(事件時間 − 提早分鐘) 比對。【關鍵修正】不再只比對「剛好那一分鐘」——
        # daemon 重啟/負載/錯過那分鐘就永遠不響。改成「到點，或過點 30 分鐘內」都補響(只響一次)。
        # 【跨午夜修正】once 提醒發出時間在 23:30 後(例如事件 00:10 提早 30 分 → 23:50)，
        # 00:05 檢查時 cur(5)<ft(1430) 會被判「還沒到」→ 該日期永遠不響。加 wrap:
        # 午夜後 30 分內、ft 在午夜前 30 分內 → 視為補響(僅 once,避免 daily 跨日重複響)。
        ft = _to_min(_fire_time(r))
        _in_window = (0 <= cur - ft <= 30)
        _wrap = (rep.startswith("once:") and cur < 30 and ft > 1410 and (cur + 1440 - ft) <= 30)
        if ft < 0 or not (_in_window or _wrap):
            remaining.append(r)
            continue

        if _is_due(r, now, today, today):
            due.append(r)
            changed = True
            if rep.startswith("once:"):
                continue  # one-shot reminder, drop it after firing
            r["last_fired"] = today

        remaining.append(r)

    if changed:
        data["reminders"] = remaining
        _save(data)

    return due
