"""Lightweight local expense ledger for the Stack-chan "語音記帳" wish-list
item.

The original wish was "語音記帳 -> Google Sheets", but that needs a Google
Cloud OAuth client + consent flow that doesn't exist in this project yet
(see SYSTEM_STATUS.md section 9). This module provides the local MVP:
`/expense <金額> <類別> [備註]` appends a row to `config/expenses.json`,
and `weekly_report()` (used by the `expense_weekly_report` scheduled
skill, see daily_content_skills.py) summarizes the last 7 days by
category. If Google Sheets sync is wanted later, it can read from this
same JSON file as its source of truth.
"""

import json
import os
from datetime import datetime, timedelta

CONFIG_PATH = "/Users/USERNAME/Hermes_Brain/config/expenses.json"


def _load():
    if not os.path.exists(CONFIG_PATH):
        return {"expenses": [], "next_id": 1, "monthly_budget": None}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
        data.setdefault("monthly_budget", None)
        return data


def _save(data):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def add_expense(amount, category, note="", date=None):
    data = _load()
    expense_id = data["next_id"]
    _now = datetime.now()
    data["expenses"].append({
        "id": expense_id,
        "amount": amount,
        "category": category,
        "note": note,
        "date": date or _now.strftime("%Y-%m-%d"),
        # 記當下時間(回溯記舊日期就不記 time)→ 讓主動引擎判斷「某餐時段有沒有記到」
        "time": None if date else _now.strftime("%H:%M"),
    })
    data["next_id"] = expense_id + 1
    _save(data)
    return expense_id


def list_recent(days=7):
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    return [e for e in _load()["expenses"] if e["date"] >= cutoff]


def remove_expense(expense_id):
    """依 id 刪掉一筆花費（語音「刪掉那筆X」用）。回傳是否真的刪到。"""
    data = _load()
    before = len(data["expenses"])
    data["expenses"] = [e for e in data["expenses"]
                        if e.get("id") != expense_id]
    _save(data)
    return len(data["expenses"]) < before


def set_monthly_budget(amount):
    data = _load()
    data["monthly_budget"] = amount
    _save(data)


def budget_remaining():
    data = _load()
    budget = data.get("monthly_budget")
    if budget is None:
        return "💰 尚未設定本月預算。用 /budget set <金額> 設定。"

    this_month = datetime.now().strftime("%Y-%m")
    spent = sum(e["amount"] for e in data["expenses"] if e["date"].startswith(this_month))
    remaining = budget - spent
    return f"💰 本月預算 {budget:,.0f} 元，已花 {spent:,.0f} 元，剩餘 {remaining:,.0f} 元"


def weekly_report():
    expenses = list_recent(days=7)
    if not expenses:
        return "📊 過去 7 天沒有任何記帳紀錄。"

    total = sum(e["amount"] for e in expenses)
    by_category = {}
    for e in expenses:
        by_category[e["category"]] = by_category.get(e["category"], 0) + e["amount"]

    lines = [f"📊 過去 7 天支出週報：共 {total:,.0f} 元"]
    for category, amount in sorted(by_category.items(), key=lambda kv: -kv[1]):
        lines.append(f"- {category}: {amount:,.0f} 元")
    return "\n".join(lines)
