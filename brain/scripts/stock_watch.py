#!/usr/bin/env python3
"""股票盤中自動異動警報（自主性，不用你先設價位）。

launchd 每 ~20 分鐘跑一次。開盤時段檢查你的持股，單檔當日漲跌幅超過門檻
就主動推 Telegram + 讓 StackChan 開口。同一檔同方向一天只提醒一次，
再往同方向多動一段(STEP)才會再提醒，不洗版。

這是「Jarvis 自己發現、主動告訴你」，不是你設好條件它才動。
"""
import json
import os
import sys
import urllib.request
from datetime import datetime
import zoneinfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from modules.finance import wealth  # noqa: E402

THRESHOLD = 5.0    # 單檔當日漲跌幅(%)超過這個 → 提醒
STEP = 3.0         # 提醒過後，要再往同方向多動這麼多 % 才再提醒一次
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(ROOT, "config", "stock_alert_state.json")
TG_CONFIG = os.path.join(ROOT, "config", "telegram.json")
MEM = "http://127.0.0.1:8809"


def _in_market_hours(now):
    """台股 09:00-13:40、美股(台灣時間)21:30-翌04:10，這兩段才檢查，省 API。"""
    hm = now.hour * 60 + now.minute
    tw = 9 * 60 <= hm <= 13 * 60 + 40
    us = hm >= 21 * 60 + 30 or hm <= 4 * 60 + 10
    return tw or us


def _load_state():
    try:
        return json.load(open(STATE_PATH))
    except Exception:
        return {}


def _save_state(s):
    json.dump(s, open(STATE_PATH, "w"), ensure_ascii=False, indent=2)


def _push_telegram(msg):
    try:
        from modules.remote.telegram_handler import TelegramHandler
        cfg = json.load(open(TG_CONFIG))
        h = TelegramHandler()
        for uid in cfg.get("allowed_user_ids", []):
            h.send_message(uid, msg)
    except Exception as e:
        print(f"tg push failed: {e}")


def _push_voice(text):
    """丟進待播語音佇列，StackChan 在旁邊就會自己唸出來。
    只在清醒時段(08:00-23:00)開口，半夜美股大動只發 Telegram、不出聲吵人。"""
    h = datetime.now(zoneinfo.ZoneInfo("Asia/Taipei")).hour
    if h < 8 or h >= 23:
        return
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"{MEM}/push_voice", data=json.dumps({"text": text}).encode(),
            headers={"Content-Type": "application/json"}), timeout=5)
    except Exception:
        pass


def main():
    now = datetime.now(zoneinfo.ZoneInfo("Asia/Taipei"))
    if not _in_market_hours(now):
        return
    try:
        pf = wealth.portfolio()
    except Exception as e:
        print(f"portfolio failed: {e}")
        return
    items = pf.get("items", [])
    if not items:
        return  # 還沒有持股，沒對象可看

    today = now.strftime("%Y-%m-%d")
    state = _load_state()
    if state.get("date") != today:
        state = {"date": today, "alerted": {}}
    alerted = state["alerted"]

    for it in items:
        if not it.get("traded_today"):
            continue
        pct = it.get("todaypct")
        if pct is None or abs(pct) < THRESHOLD:
            continue
        sym = it.get("symbol")
        last = alerted.get(sym)
        # 已提醒過：同方向且沒再多動 STEP% → 跳過(不洗版)
        if last is not None and (pct > 0) == (last > 0) and abs(pct - last) < STEP:
            continue
        nm = it.get("name") or sym
        direction = "大漲" if pct > 0 else "大跌"
        _push_telegram(f"⚡ {nm}（{sym}）今天{direction} {pct:+.2f}%，要不要看一下？")
        _push_voice(f"欸 Owen，提醒你一下，{nm}今天{direction} {abs(pct):.1f} 趴。")
        alerted[sym] = pct
        print(f"alerted {sym} {pct:+.2f}%")

    state["alerted"] = alerted
    _save_state(state)


if __name__ == "__main__":
    main()
