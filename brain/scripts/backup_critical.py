"""關鍵資料自動備份。

備份 Owen 最不能遺失的東西：錢(finance/expenses)、記憶(facts)、提醒、待辦、身份。
複製到 ~/.hermes/backups/YYYYMMDD_HHMMSS/，保留最近 40 份(約夠回溯數週)。
純複製、絕不刪原檔；備份區也只輪替刪「舊備份」，不碰任何正式資料。
"""
from __future__ import annotations

import os
import shutil
import time

HOME = os.path.expanduser("~")
HB = os.path.join(HOME, "Hermes_Brain")
BACKUP_ROOT = os.path.join(HOME, ".hermes", "backups")
KEEP = 40

# (來源絕對路徑, 備份後檔名)
SOURCES = [
    (os.path.join(HB, "config", "finance.json"), "finance.json"),
    (os.path.join(HB, "config", "expenses.json"), "expenses.json"),
    (os.path.join(HB, "config", "reminders.json"), "reminders.json"),
    (os.path.join(HB, "config", "checklists.json"), "checklists.json"),
    (os.path.join(HB, "config", "todos.json"), "todos.json"),
    (os.path.join(HOME, ".hermes", "memories", "facts.jsonl"), "facts.jsonl"),
    (os.path.join(HOME, ".hermes", "memories", "current_identity.json"), "current_identity.json"),
]


def _stamp() -> str:
    # launchd 每次跑是新進程，time.time() 可用（不是 workflow 沙箱）
    return time.strftime("%Y%m%d_%H%M%S")


def run_once() -> int:
    os.makedirs(BACKUP_ROOT, exist_ok=True)
    dest = os.path.join(BACKUP_ROOT, _stamp())
    os.makedirs(dest, exist_ok=True)
    n = 0
    for src, name in SOURCES:
        if os.path.exists(src):
            try:
                shutil.copy2(src, os.path.join(dest, name))
                n += 1
            except Exception as e:
                print(f"[backup] {name} 失敗: {e}")
    # 沒備到任何東西 → 別留空資料夾
    if n == 0:
        try:
            os.rmdir(dest)
        except Exception:
            pass
        return 0
    # 輪替：只刪最舊的「備份資料夾」，永不碰正式資料
    try:
        dirs = sorted(d for d in os.listdir(BACKUP_ROOT)
                      if os.path.isdir(os.path.join(BACKUP_ROOT, d)))
        for old in dirs[:-KEEP]:
            shutil.rmtree(os.path.join(BACKUP_ROOT, old), ignore_errors=True)
    except Exception:
        pass
    print(f"[backup] {dest} 備份了 {n} 個檔")
    return n


if __name__ == "__main__":
    run_once()
