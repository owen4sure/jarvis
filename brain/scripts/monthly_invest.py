#!/usr/bin/env python3
"""每月固定把『存款目標金額』記成一筆投入投資。由 launchd 每月 1 號跑。
同一個月只會記一次（重複跑不會重複加）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.finance import wealth  # noqa: E402


def main():
    total = wealth.record_monthly_invest()
    amount, months = 0, 0
    log = wealth.load().get("invest_log", [])
    if log:
        amount = log[-1].get("amount", 0)
        months = len(log)
    print(f"已記錄本月投入 {amount}，累積 {total}（{months} 個月）")
    # 推 Telegram 通知（有設定才推）
    try:
        import json
        from modules.remote.telegram_handler import TelegramHandler
        cfg = json.load(open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config", "telegram.json")))
        handler = TelegramHandler()
        msg = (f"💸 *每月投資提醒*\n本月已把 NT${int(amount):,} 列入固定投入投資，"
               f"累積投入 NT${int(total):,}（共 {months} 個月）。\n記得實際把錢轉到券商並買進喔！")
        for uid in cfg.get("allowed_user_ids", []):
            try:
                handler.send_message(uid, msg, parse_mode="Markdown")
            except Exception:
                pass
    except Exception:
        pass


if __name__ == "__main__":
    main()
