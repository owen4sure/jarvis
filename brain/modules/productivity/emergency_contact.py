"""Emergency-contact registry for the "深夜未回通知緊急聯絡人" wish-list item.

`/emergency set <chat_id> [名字]` registers someone's Telegram chat_id
(they get their own chat_id by messaging any bot, e.g. @userinfobot, or
by messaging this bot once - an unauthorized user gets told their
user_id, which equals their private chat_id). `late_night_check()` is
called by a `late_night_checkin` watcher (see `watcher_checks.py`): if
the user hasn't sent the Hermes Telegram bot any message all day by the
configured cutoff time, it messages the emergency contact directly.

Storage: `config/emergency_contact.json`:
    {"chat_id": 123456789, "name": "媽媽"}
"""

import json
import os

CONFIG_PATH = "/Users/chenyouwei/Hermes_Brain/config/emergency_contact.json"


def _load():
    if not os.path.exists(CONFIG_PATH):
        return None
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_contact():
    return _load()


def set_contact(chat_id, name=""):
    _save({"chat_id": chat_id, "name": name})


def remove_contact():
    if os.path.exists(CONFIG_PATH):
        os.remove(CONFIG_PATH)
        return True
    return False
