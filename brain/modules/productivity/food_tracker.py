"""Ingredient/food expiry tracker for the Stack-chan "食材到期管理"
wish-list item.

`/food add <品項> <YYYY-MM-DD>` stores an entry in `config/food.json`.
`expiring_soon()` (used by the `food_expiry_check` scheduled skill, see
daily_content_skills.py) lists items expiring within `days` days,
removing items once they've expired so the list doesn't grow forever.
"""

import json
import os
from datetime import datetime, timedelta

CONFIG_PATH = "/Users/USERNAME/Hermes_Brain/config/food.json"


def _load():
    if not os.path.exists(CONFIG_PATH):
        return {"items": []}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def add_item(name, expiry_date):
    datetime.strptime(expiry_date, "%Y-%m-%d")  # raises ValueError if invalid

    data = _load()
    data["items"].append({"name": name, "expiry": expiry_date})
    _save(data)


def list_items():
    return _load()["items"]


def remove_item(name):
    data = _load()
    before = len(data["items"])
    data["items"] = [i for i in data["items"] if i["name"] != name]
    _save(data)
    return len(data["items"]) < before


def expiring_soon(days=3):
    """Returns items expiring within `days` days (including already
    expired). Removes already-expired items from storage as a side
    effect, so they're only reported once."""
    today = datetime.now().date()
    horizon = today + timedelta(days=days)

    data = _load()
    soon = []
    remaining = []
    for item in data["items"]:
        expiry = datetime.strptime(item["expiry"], "%Y-%m-%d").date()
        if expiry <= horizon:
            soon.append(item)
            if expiry >= today:
                remaining.append(item)  # keep until actually expired
        else:
            remaining.append(item)

    if len(remaining) != len(data["items"]):
        data["items"] = remaining
        _save(data)

    return soon
