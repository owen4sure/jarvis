"""Content generators for `skill`-backed reminders (see
`reminder_manager.py`). Each function takes no arguments and returns the
message body `reminder_daemon.py` should send when that reminder fires.

Covers several Stack-chan wish-list items that just need "generate
something at a scheduled time", reusing the reminder scheduler:
- 每日單字/語言沉浸 -> daily_word
- 每日思考題 -> daily_thought
- RSS/HN/Product Hunt 精選 -> daily_digest
- 財務週報 -> expense_weekly_report (modules/productivity/expense_tracker.py)
- 每日情緒趨勢 -> mood_weekly_trend (modules/productivity/mood_tracker.py)
"""

import json
import urllib.request

from modules.embodied.gemini_client import GeminiClient
from modules.productivity import contact_tracker, expense_tracker, food_tracker, mood_tracker, sleep_tracker

HN_TOP_STORIES_URL = "https://hacker-news.firebaseio.com/v0/topstories.json"
HN_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{id}.json"


def daily_word():
    prompt = (
        "請給我一個今天的英文單字學習小卡，適合口語播報。"
        "包含：1個實用英文單字、音標、中文意思、一個例句（含中文翻譯）。"
        "用簡短的繁體中文文字輸出，不要用 markdown 標記，3-5 句話以內。"
    )
    return "📚 今日單字\n" + GeminiClient().chat(prompt)


def daily_thought():
    prompt = (
        "請給我一個今天的簡短思考題，適合早上聽到後想一整天。"
        "主題可以是哲學、人生、科技、或自我成長，問題本身要有趣且開放。"
        "只輸出問題本身（可加一句簡短引導），繁體中文，2-3 句話以內，不要用 markdown。"
    )
    return "🤔 今日思考題\n" + GeminiClient().chat(prompt)


def daily_digest():
    try:
        with urllib.request.urlopen(HN_TOP_STORIES_URL, timeout=8) as resp:
            top_ids = json.load(resp)[:5]
    except Exception as e:
        return f"📰 今日精選讀取失敗: {e}"

    lines = ["📰 今日 Hacker News 精選"]
    for item_id in top_ids:
        try:
            with urllib.request.urlopen(HN_ITEM_URL.format(id=item_id), timeout=8) as resp:
                item = json.load(resp)
            title = item.get("title", "(無標題)")
            score = item.get("score", 0)
            lines.append(f"- {title} ({score} 分)")
        except Exception:
            continue

    if len(lines) == 1:
        return "📰 今日精選讀取失敗：沒有可用的項目。"
    return "\n".join(lines)


def expense_weekly_report():
    return expense_tracker.weekly_report()


def mood_weekly_trend():
    return mood_tracker.weekly_trend()


def budget_remaining():
    return expense_tracker.budget_remaining()


def food_expiry_check():
    items = food_tracker.expiring_soon(days=3)
    if not items:
        return "🥬 食材檢查：目前沒有即將過期的食材。"

    lines = ["🥬 食材檢查：以下食材即將過期或已過期"]
    for item in items:
        lines.append(f"- {item['name']}（到期日 {item['expiry']}）")
    return "\n".join(lines)


def contact_check():
    due = contact_tracker.due_for_contact()
    if not due:
        return "👥 聯絡提醒：目前沒有人需要聯絡。"

    lines = ["👥 聯絡提醒：好久沒聯絡這些人了"]
    for c in due:
        lines.append(f"- {c['name']}（{c['days_since']} 天沒聯絡了）")
    return "\n".join(lines)


def bedtime_check():
    return sleep_tracker.bedtime_check()


def family_schedule_check():
    from modules.productivity import calendar_sync
    try:
        events = calendar_sync.upcoming_events(days=1)
    except calendar_sync.CalendarNotConfiguredError:
        # Not configured yet - skip silently rather than nagging every
        # day (same as earthquake/typhoon watchers before config/cwa.json).
        return None
    except Exception as e:
        return f"⚠️ 讀取行事曆失敗: {e}"

    if not events:
        return "📅 接下來 1 天沒有任何行程。"

    lines = ["📅 接下來 1 天的行程："]
    for e in events:
        lines.append(f"- {e['start']}: {e['summary']}")
    return "\n".join(lines)


SKILLS = {
    "daily_word": daily_word,
    "daily_thought": daily_thought,
    "daily_digest": daily_digest,
    "expense_weekly_report": expense_weekly_report,
    "mood_weekly_trend": mood_weekly_trend,
    "budget_remaining": budget_remaining,
    "food_expiry_check": food_expiry_check,
    "contact_check": contact_check,
    "bedtime_check": bedtime_check,
    "family_schedule_check": family_schedule_check,
}


def generate(skill_name):
    """Returns the message body for `skill_name`, or an error string if
    the skill name is unknown."""
    func = SKILLS.get(skill_name)
    if func is None:
        return f"⚠️ 未知的提醒 skill: {skill_name}"
    try:
        return func()
    except Exception as e:
        return f"⚠️ 產生 {skill_name} 內容失敗: {e}"
