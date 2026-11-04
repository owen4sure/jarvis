"""
每日簡報 — 主動推播（讓 Hermes 會「主動關心」，不只被動回答）
============================================================================
組合：日期問候 + 今日提醒 + 食材到期 + 久未聯絡的人 + 預算 + 今日一則思考，
推播到 Telegram。每一段都獨立 try/except，缺設定也不會整份壞掉。

    ./.venv/bin/python -m scripts.daily_briefing            # 真的推播
    ./.venv/bin/python -m scripts.daily_briefing --dry-run  # 只印不送（測試用）

排程：launchd com.hermes.dailybriefing（每天早上 08:00）。
"""
import sys
from datetime import datetime

from modules.productivity import daily_content_skills as dc
from modules.productivity import reminder_manager

_WEEKDAY_ZH = ["一", "二", "三", "四", "五", "六", "日"]


def _safe(fn, *a):
    try:
        out = fn(*a)
        return out.strip() if isinstance(out, str) else out
    except Exception as e:
        return None


def _today_reminders():
    try:
        rs = reminder_manager.list_reminders()
        if not rs:
            return None
        lines = []
        for r in rs:
            msg = r.get("message") or r.get("text") or ""
            t = r.get("time") or r.get("time_str") or ""
            if msg:
                lines.append(f"  • {t} {msg}".rstrip())
        return "🔔 今日提醒：\n" + "\n".join(lines) if lines else None
    except Exception:
        return None


def build_briefing() -> str:
    now = datetime.now()
    header = (f"☀️ 早安！{now.year}/{now.month}/{now.day} "
              f"星期{_WEEKDAY_ZH[now.weekday()]} {now.strftime('%H:%M')}")

    sections = [header]
    for label, val in [
        (None, _today_reminders()),
        ("🍳", _safe(dc.food_expiry_check)),
        ("👥", _safe(dc.contact_check)),
        ("💰", _safe(dc.budget_remaining)),
        ("💡", _safe(dc.daily_thought)),
    ]:
        if val:
            sections.append(f"{label} {val}" if label and not str(val).startswith(label) else str(val))

    if len(sections) == 1:
        sections.append("今天沒有特別提醒，祝你有美好的一天 🌿")
    return "\n\n".join(sections)


def main():
    dry = "--dry-run" in sys.argv
    text = build_briefing()
    if dry:
        print("---- DRY RUN（不會推播）----\n")
        print(text)
        return
    from modules.remote.telegram_handler import TelegramHandler
    import json, os
    cfg = json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                      "config", "telegram.json")))
    handler = TelegramHandler()
    for uid in cfg.get("allowed_user_ids", []):
        try:
            handler.send_message(uid, text)  # 私聊時 chat_id == user_id
        except Exception as e:
            print(f"⚠️ 推播給 {uid} 失敗: {e}")

    # StackChan 在旁邊就順便用講的報一聲早安（不在就略過）
    try:
        from modules.embodied import notify
        notify.speak_if_present("早安！今天的簡報我傳到你的 Telegram 了。")
    except Exception:
        pass


if __name__ == "__main__":
    main()
