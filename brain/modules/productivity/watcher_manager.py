"""Generic "watch for a condition, alert once when it changes" store.

Covers the Stack-chan "感知世界" wish-list items that are event-driven
rather than scheduled-at-a-fixed-time (so `reminder_manager.py`'s
HH:MM scheduling doesn't fit): price alerts, earthquake/typhoon alerts.
`scripts/reminder_daemon.py` calls `check_watchers()` every cycle
(60s) alongside the daily reminder check.

Storage: `config/watchers.json`:
    {
        "watchers": [
            {"id": 1, "type": "crypto", "coin_id": "bitcoin",
             "condition": "above", "price": 100000, "vs_currency": "usd",
             "fired": false}
        ],
        "next_id": 2
    }

The actual condition-checking logic for each `type` lives in
`watcher_checks.py` to keep this file focused on storage/CRUD.
"""

import json
import os

CONFIG_PATH = "/Users/USERNAME/Hermes_Brain/config/watchers.json"

SUPPORTED_TYPES = ("crypto", "stock", "earthquake", "typhoon", "flight", "late_night_checkin")


def _load():
    if not os.path.exists(CONFIG_PATH):
        return {"watchers": [], "next_id": 1}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def list_watchers():
    return _load()["watchers"]


def add_watcher(watcher):
    """`watcher` is a dict without "id" (caller fills type-specific
    fields). Returns the new watcher's id."""
    if watcher.get("type") not in SUPPORTED_TYPES:
        raise ValueError(f"不支援的 watcher 類型: {watcher.get('type')!r}")

    data = _load()
    watcher_id = data["next_id"]
    watcher["id"] = watcher_id
    data["watchers"].append(watcher)
    data["next_id"] = watcher_id + 1
    _save(data)
    return watcher_id


def remove_watcher(watcher_id):
    data = _load()
    before = len(data["watchers"])
    data["watchers"] = [w for w in data["watchers"] if w["id"] != watcher_id]
    _save(data)
    return len(data["watchers"]) < before


def update_watcher(watcher_id, **fields):
    data = _load()
    for w in data["watchers"]:
        if w["id"] == watcher_id:
            w.update(fields)
            _save(data)
            return True
    return False


def check_watchers():
    """Runs every watcher's condition check, returns a list of alert
    message strings for watchers whose condition newly became true."""
    from . import watcher_checks  # local import: avoids import cost when unused

    data = _load()
    alerts = []
    changed = False
    for w in data["watchers"]:
        try:
            result = watcher_checks.check(w)
        except Exception as e:
            result = None
            print(f"⚠️ [WatcherManager] 檢查 watcher #{w['id']} 失敗: {e}")

        if result and result.get("message"):
            alerts.append(result["message"])
        if result and result.get("updates"):
            w.update(result["updates"])
            changed = True

    if changed:
        _save(data)

    return alerts
