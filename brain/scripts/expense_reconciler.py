"""記帳對帳器（確定性安全網）。

問題：長對話中 flash-lite 常模仿前句「記好了」卻沒真的呼叫 add_expense（已驗證），
所以使用者報的帳偶爾會漏。在 proxy 熱路徑攔截會把 8809 灌爆、不可行。

做法：背景每 60 秒掃 state.db 新的 user 訊息（語音+Telegram 都在這），
凡是「純品項+金額」的報帳、而今天帳上找不到對應那筆 → 補記。完全脫離請求熱路徑、零風險。

去重：只看「今天有沒有同金額+同品項」，有就不補（模型已記）；沒有才補。自帶游標只處理新訊息。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import urllib.request

STATE_DB = os.path.expanduser("~/.hermes/state.db")
CURSOR = os.path.expanduser("~/.hermes/.expense_cursor")
MEM = "http://127.0.0.1:8809"

# 純「品項+金額」才算報帳；帶問句/比較/時間單位的不是。
_PAT = re.compile(r"^([一-龥A-Za-z]{1,8})\s*(\d{1,6})$")
_BAD = re.compile(r"[嗎?？比貴便宜划算多少幾]|提醒|分鐘|小時|號|樓|歲|公斤|度|測試"
                  r"|刪|删|移除|取消|改成|更新|查|問|幾點|剛剛那筆|那筆|壓測|压测")
_FOOD = ("餐", "飯", "麵", "茶", "咖啡", "飲", "便當", "超商", "全家", "美廉",
         "便利", "早", "午", "晚", "吃", "店", "食", "店")


def _unwrap(text: str) -> str:
    """語音訊息可能是 {"speaker","content"} JSON → 取 content。"""
    t = (text or "").strip()
    if t.startswith("{") and '"content"' in t:
        try:
            return (json.loads(t).get("content") or "").strip()
        except Exception:
            return t
    return t


def _parse(text: str):
    """回 (品項, 金額) 或 None。"""
    t = _unwrap(text)
    if _BAD.search(t):
        return None
    m = _PAT.match(t)
    if not m:
        return None
    amt = int(m.group(2))
    # 金額要在日常合理範圍(5~8000)。太大八成是 ID/電話/亂數(如「是sandy135420」)被誤當花費 → 不自動補;
    # 真的大額支出(房租等)請用語音明確記(add_expense 沒這限制)。
    if amt < 5 or amt > 8000:
        return None
    return m.group(1), amt


def _cursor() -> int:
    try:
        return int(open(CURSOR).read().strip())
    except Exception:
        return 0


def _save_cursor(n: int) -> None:
    open(CURSOR, "w").write(str(n))


def _new_user_msgs(after: int):
    con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=5)
    try:
        rows = con.execute(
            "SELECT id, content FROM messages WHERE role='user' AND id>? ORDER BY id ASC",
            (after,)).fetchall()
    finally:
        con.close()
    return rows


def _today_expenses():
    try:
        d = json.load(open(os.path.expanduser("~/Hermes_Brain/config/expenses.json")))
        t = time.strftime("%Y-%m-%d")
        return [e for e in d.get("expenses", []) if str(e.get("date", "")).startswith(t)]
    except Exception:
        return []


def _already(item: str, amt: int, today: list) -> bool:
    """今天有沒有記過這筆（金額相同 + 品項有重疊就算）。"""
    for e in today:
        if int(float(e.get("amount") or 0)) == amt:
            note = str(e.get("note", "")) + str(e.get("category", ""))
            if item in note or note in item or not item:
                return True
    return False


def _record(item: str, amt: int) -> None:
    cat = "餐飲" if any(k in item for k in _FOOD) else "其他"
    req = urllib.request.Request(
        f"{MEM}/expense",
        data=json.dumps({"amount": amt, "note": item, "category": cat}).encode(),
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=8)


def reconcile_one(text: str) -> bool:
    """對單一訊息做「品項+金額」記帳補漏(嚴格模式+去重)。回傳是否有補記。
    給語音橋接即時呼叫 → 使用者一報帳就補,不必等 60 秒背景掃描,也不靠 flash-lite 有沒有呼叫工具。"""
    p = _parse(text or "")
    if not p:
        return False
    if _already(p[0], p[1], _today_expenses()):
        return False   # 模型已記(或剛補過) → 不重複
    try:
        _record(p[0], p[1])
        print(f"[reconcile-now] 即時補記 {p[0]} {p[1]}元")
        return True
    except Exception:
        return False


def run_once():
    after = _cursor()
    rows = _new_user_msgs(after)
    if not rows:
        return 0
    today = _today_expenses()
    fixed = 0
    for mid, content in rows:
        p = _parse(content or "")
        if p and not _already(p[0], p[1], today):
            try:
                _record(p[0], p[1])
                today = _today_expenses()  # 重抓，避免同輪重複補
                fixed += 1
                print(f"[reconcile] 補記 {p[0]} {p[1]}元")
            except Exception as e:
                print(f"[reconcile] 補記失敗 {p}: {e}")
    _save_cursor(rows[-1][0])
    return fixed


if __name__ == "__main__":
    run_once()
