"""Simple named checklists for the Stack-chan "出門清單" (and similar)
wish-list item.

Storage: `config/checklists.json`:
    {"out": ["鑰匙", "錢包", "手機", "悠遊卡"]}

`/checklist` (no args) shows the default "out" list; `/checklist add/remove`
edit it. Designed so other named lists (e.g. "packing") could reuse the
same storage if needed later.
"""

import json
import os

CONFIG_PATH = "/Users/chenyouwei/Hermes_Brain/config/checklists.json"

DEFAULT_LIST_NAME = "out"
DEFAULT_ITEMS = ["鑰匙", "錢包", "手機", "悠遊卡"]


def _load():
    if not os.path.exists(CONFIG_PATH):
        return {DEFAULT_LIST_NAME: list(DEFAULT_ITEMS)}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_items(list_name=DEFAULT_LIST_NAME):
    return _load().get(list_name, [])


def add_item(item, list_name=DEFAULT_LIST_NAME):
    data = _load()
    items = data.setdefault(list_name, [])
    if item not in items:
        items.append(item)
    _save(data)
    return items


def remove_item(item, list_name=DEFAULT_LIST_NAME):
    data = _load()
    items = data.get(list_name, [])
    if item in items:
        items.remove(item)
        _save(data)
        return True
    return False
