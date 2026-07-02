"""喝水紀錄的輕量本地帳本（仿 expense_tracker.py 的寫法）。

語音說「喝水」就加一杯，紀錄寫進 config/water.json，
每天各自累計、可查任一天喝了幾杯。
"""

import json
import os
from datetime import datetime

CONFIG_PATH = "/Users/chenyouwei/Hermes_Brain/config/water.json"


def _load():
    if not os.path.exists(CONFIG_PATH):
        return {"logs": []}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def add_cup(cups=1, date=None):
    """加喝水紀錄，預設加一杯。回傳加完後當天的總杯數。"""
    data = _load()
    _now = datetime.now()
    d = date or _now.strftime("%Y-%m-%d")
    data["logs"].append({
        "date": d,
        "cups": cups,
        "time": _now.strftime("%H:%M"),
    })
    _save(data)
    return count_for_date(d, data=data)


def count_for_date(date=None, data=None):
    """查某一天喝了幾杯（沒給日期＝今天）。"""
    data = data or _load()
    d = date or datetime.now().strftime("%Y-%m-%d")
    return sum(r.get("cups", 0) for r in data["logs"] if r.get("date") == d)


def recent_days(days=7):
    """最近 N 天每天的杯數，dashboard 面板用。"""
    data = _load()
    from datetime import timedelta
    today = datetime.now().date()
    out = []
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        out.append({"date": d, "cups": count_for_date(d, data=data)})
    return out
