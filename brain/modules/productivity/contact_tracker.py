"""Local "haven't talked to X in a while" tracker for the Stack-chan
"久未聯絡提醒 / 家人行程關注" wish-list items.

A full implementation of those wishes would pull from Google Contacts/
Calendar, but that needs a Google Cloud OAuth client (see
SYSTEM_STATUS.md section 9). This module provides the local MVP that
needs no external account: `/contact add <名字> [幾天提醒一次=7]` registers
someone, `/contact touch <名字>` marks "talked today", and
`due_for_contact()` (used by the `contact_check` scheduled skill, see
daily_content_skills.py) lists anyone overdue.

Storage: `config/contacts.json`:
    {"contacts": [{"name": "媽媽", "remind_after_days": 7, "last_contact": "2026-06-10"}]}
"""

import json
import os
from datetime import datetime

CONFIG_PATH = "/Users/chenyouwei/Hermes_Brain/config/contacts.json"


def _load():
    if not os.path.exists(CONFIG_PATH):
        return {"contacts": []}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def list_contacts():
    return _load()["contacts"]


def add_contact(name, remind_after_days=7):
    data = _load()
    for c in data["contacts"]:
        if c["name"] == name:
            c["remind_after_days"] = remind_after_days
            _save(data)
            return
    data["contacts"].append({
        "name": name,
        "remind_after_days": remind_after_days,
        "last_contact": datetime.now().strftime("%Y-%m-%d"),
    })
    _save(data)


def touch_contact(name):
    data = _load()
    for c in data["contacts"]:
        if c["name"] == name:
            c["last_contact"] = datetime.now().strftime("%Y-%m-%d")
            _save(data)
            return True
    return False


def remove_contact(name):
    data = _load()
    before = len(data["contacts"])
    data["contacts"] = [c for c in data["contacts"] if c["name"] != name]
    _save(data)
    return len(data["contacts"]) < before


def due_for_contact():
    today = datetime.now().date()
    due = []
    for c in _load()["contacts"]:
        last = datetime.strptime(c["last_contact"], "%Y-%m-%d").date()
        days_since = (today - last).days
        if days_since >= c.get("remind_after_days", 7):
            due.append({**c, "days_since": days_since})
    return due
