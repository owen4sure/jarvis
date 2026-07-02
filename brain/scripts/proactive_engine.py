#!/usr/bin/env python3
"""Jarvis 主動引擎 —— 統一決定「現在值不值得主動找 Owen、講哪件、用什麼管道」。

launchd 每 ~30 分鐘跑一次。各 checker 各自看自己的領域(記帳/預算/信件/行程…)，
回傳 0..N 個 nudge；引擎統一套守則(安靜時段、每日上限、同件事不重複)後才送出。

新增主動行為 = 寫一個 checker 函式加進 CHECKERS，不用碰引擎本體。

nudge 格式:
  {"key": 去重鍵(同鍵一天只發一次), "urgency": "urgent"|"general",
   "tg": Telegram 文字, "voice": 要不要開口講的話(None=不開口)}
"""
import json
import os
import sys
from datetime import datetime
import zoneinfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(ROOT, "config", "proactive_state.json")
TG_CONFIG = os.path.join(ROOT, "config", "telegram.json")
MEM = "http://127.0.0.1:8809"
TZ = zoneinfo.ZoneInfo("Asia/Taipei")

QUIET_START, QUIET_END = 0, 8     # 00:00–08:00 完全不主動
VOICE_START, VOICE_END = 8, 23    # 語音只在 08:00–23:00 出聲
DAILY_GENERAL_CAP = 4             # 一般(非緊急)主動，一天上限


# ───────────────────────── 工具 ─────────────────────────
def _state():
    try:
        return json.load(open(STATE_PATH))
    except Exception:
        return {}


def _save_state(s):
    json.dump(s, open(STATE_PATH, "w"), ensure_ascii=False, indent=2)


def _push_tg(msg):
    try:
        from modules.remote.telegram_handler import TelegramHandler
        cfg = json.load(open(TG_CONFIG))
        h = TelegramHandler()
        for uid in cfg.get("allowed_user_ids", []):
            h.send_message(uid, msg)
        return True
    except Exception as e:
        print(f"tg failed: {e}")
        return False


def _push_voice(text, now):
    import urllib.request
    if not (VOICE_START <= now.hour < VOICE_END):
        return
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"{MEM}/push_voice", data=json.dumps({"text": text}).encode(),
            headers={"Content-Type": "application/json"}), timeout=5)
    except Exception:
        pass


def _today_expenses(now):
    try:
        from modules.productivity import expense_tracker as et
        data = et._load()
        today = now.strftime("%Y-%m-%d")
        return [e for e in data.get("expenses", []) if e.get("date") == today]
    except Exception:
        return []


def _overview():
    try:
        from modules.finance import wealth
        return wealth.overview()
    except Exception:
        return {}


# ───────────────────────── Checkers ─────────────────────────
def check_expense_gap(now):
    """正常會花錢的時段過了卻沒記到 → 問一下。午餐後、晚餐後各看一次。"""
    out = []
    exp = _today_expenses(now)
    # 【誤判修正】category 常被歸成「其他」(如晚餐140元被記成其他)，只看 category=="餐飲"
    # 會把明明記過的餐當成漏記、亂吵人(已犯過)。改成看 note+category 的「內容」判斷是不是餐。
    _MEAL = ("餐", "飯", "麵", "吃", "便當", "宵夜", "早", "午", "晚", "咖啡", "茶", "飲", "食", "喝",
             # 常見店家/品項名（category 常是「其他」、note 只寫店名）→ 也算吃飯，免得誤報漏記
             "麥當勞", "麥當", "肯德基", "kfc", "subway", "星巴克", "摩斯", "漢堡", "披薩", "pizza",
             "foodpanda", "熊貓", "ubereats", "uber eat", "拉麵", "火鍋", "鹹酥雞", "鹽酥雞", "滷味",
             "自助餐", "brunch", "水餃", "小吃", "牛肉", "雞排", "壽司", "拉茶", "手搖", "全家", "７-11", "7-11", "超商")
    def _blob(e):
        return (str(e.get("category", "")) + str(e.get("note", ""))).lower()
    def _is_meal(e):
        return e.get("category") == "餐飲" or any(k in _blob(e) for k in _MEAL)
    def _has(*kw):
        return any(any(k.lower() in _blob(e) for k in kw) for e in exp)
    # 備援：就算沒對到任何餐點關鍵字，只要今天有「其他」類、金額落在一餐合理範圍(40~500)的花費，
    # 也當作「可能吃過了」→ 寧可少吵，不要對記過的人誤報漏記(使用者被誤報過、很反感)。
    def _maybe_meal(e):
        try:
            amt = int(float(e.get("amount") or 0))
        except Exception:
            amt = 0
        return _is_meal(e) or (40 <= amt <= 500)
    food = [e for e in exp if _maybe_meal(e)]
    hm = now.hour * 60 + now.minute
    # 午餐:13:30–15:00 之間檢查，今天完全沒有任何餐 → 問
    if 13 * 60 + 30 <= hm <= 15 * 60 and len(food) == 0:
        out.append({"key": "exp_lunch", "urgency": "general",
                    "tg": "🍱 中午吃飯了吧？好像還沒看到你記中餐，花多少？跟我說我幫你記。",
                    "voice": "欸 Owen，中午吃飯了嗎？還沒記到帳喔，花多少跟我說。"})
    # 晚餐:20:00–21:30。雙保險免誤判:有「晚餐/晚飯/宵夜」字樣、或今天已記≥2餐 → 就當記過了、不吵。
    elif (20 * 60 <= hm <= 21 * 60 + 30
          and not _has("晚餐", "晚飯", "晚飲", "宵夜", "dinner")
          and len(food) < 2):
        out.append({"key": "exp_dinner", "urgency": "general",
                    "tg": "🍜 晚餐記了嗎？今天好像漏記了，多少錢跟我說一聲。",
                    "voice": "Owen，晚餐花的錢記了嗎？漏了的話跟我說。"})
    return out


def check_budget_low(now):
    """每天可花掉到 400 以下 → 提醒省一點。"""
    ov = _overview()
    da = ov.get("daily_allowance")
    if isinstance(da, (int, float)) and da < 400:
        rem, days = ov.get("remaining"), ov.get("days_left")
        return [{"key": "budget_low", "urgency": "general",
                 "tg": f"💸 提醒一下：你現在每天可花剩 {int(da)} 元了"
                       + (f"（本期還剩 {int(rem)}，要撐 {days} 天）" if rem else "")
                       + "，接下來省著點。",
                 "voice": f"Owen 提醒你，每天可花的預算剩 {int(da)} 元了，最近省一點喔。"}]
    return []


def check_payday(now):
    """發薪日當天早上，提醒把「發薪先存」的固定投資金額轉出去（紀律別忘）。"""
    ov = _overview()
    payday = int(ov.get("payday") or 15)
    saved = ov.get("auto_saved")
    if now.day == payday and 7 <= now.hour <= 11 and saved:
        return [{"key": "payday", "urgency": "general",
                 "tg": f"💰 今天 {payday} 號發薪日！記得先把這個月要投資的 {int(saved)} 元轉到證券戶（發薪先存的紀律）。",
                 "voice": f"Owen，今天發薪日，記得把要投資的 {int(saved)} 元先轉出去喔。"}]
    return []


def check_spend_pace(now):
    """花費速度超過時間進度太多 → 提早預警（比 budget_low 早發現，還沒爆就先提醒）。"""
    ov = _overview()
    try:
        cs = datetime.strptime(str(ov.get("cycle_start"))[:10], "%Y-%m-%d")
        ce = datetime.strptime(str(ov.get("cycle_end"))[:10], "%Y-%m-%d")
        total = (ce - cs).days or 1
        elapsed = max(0.0, (now.replace(tzinfo=None) - cs).days) / total
        spendable = float(ov.get("spendable") or 0)
        spent = float(ov.get("month_var") or 0)
        if spendable > 0:
            sf = spent / spendable
            if 0.25 <= elapsed and sf >= elapsed + 0.30 and sf < 1.0:  # 花得比時間快 30%+ 且還沒爆
                return [{"key": "spend_pace", "urgency": "general",
                         "tg": f"⏱️ 本期才過 {int(elapsed*100)}%，預算卻已用掉 {int(sf*100)}%，花得有點快，後面留意一下～",
                         "voice": f"Owen，這期你花得有點快，才過 {int(elapsed*100)}% 就用了 {int(sf*100)}% 的預算，後面省一點喔。"}]
    except Exception:
        pass
    return []


CHECKERS = [check_expense_gap, check_budget_low, check_payday, check_spend_pace]


# ───────────────────────── 引擎 ─────────────────────────
def run():
    now = datetime.now(TZ)
    if QUIET_START <= now.hour < QUIET_END:
        return  # 安靜時段，完全不主動

    today = now.strftime("%Y-%m-%d")
    st = _state()
    if st.get("date") != today:
        st = {"date": today, "sent": {}, "general": 0}
    sent = st["sent"]

    candidates = []
    for chk in CHECKERS:
        try:
            candidates += chk(now) or []
        except Exception as e:
            print(f"{chk.__name__} failed: {e}")

    for c in candidates:
        key = c["key"]
        if key in sent:
            continue  # 今天這件事已經提過
        if c.get("urgency") != "urgent" and st["general"] >= DAILY_GENERAL_CAP:
            continue  # 一般主動已達每日上限
        if _push_tg(c["tg"]):
            if c.get("voice"):
                _push_voice(c["voice"], now)
            sent[key] = now.strftime("%H:%M")
            if c.get("urgency") != "urgent":
                st["general"] += 1
            print(f"nudged {key}")

    st["sent"] = sent
    _save_state(st)


if __name__ == "__main__":
    run()
