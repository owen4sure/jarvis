"""Claude Code bridge for self-development.

Allows the Telegram bot to invoke Claude Code as a subprocess to implement
new features. Restricted to /Users/chenyouwei/Hermes_Brain only.
"""

import os
import subprocess
import threading
import time
from typing import Callable

CLAUDE_BIN = "/Users/chenyouwei/.local/bin/claude"
HERMES_DIR = "/Users/chenyouwei/Hermes_Brain"
TIMEOUT_SECONDS = 600  # 10 minutes max

_SYSTEM_PROMPT = """你是 Hermes Brain 的功能開發助理。

硬性規則：
1. 只能讀寫 /Users/chenyouwei/Hermes_Brain 目錄內的檔案，絕對不可動其他目錄
2. 新指令要整合進 modules/remote/commands.py 的 COMMAND_TABLE
3. 新模組放在對應的 modules/ 子目錄
4. 不可修改 config/keys.json 等機密設定檔
5. 每新增一個指令，必須同時在 modules/remote/commands.py 的 COMMAND_DESCRIPTIONS
   字典新增一筆描述（格式："command_name": "中文描述 [參數]: 觸發例子"），
   否則自然語言意圖路由器無法辨識該功能
6. 完成後輸出摘要：新增/修改了哪些檔案、新增了什麼指令或功能

"""


def run_dev_task(
    task: str,
    progress_callback: Callable[[str], None],
    done_callback: Callable[[bool, str], None],
) -> None:
    """Spawn Claude Code in a background thread to implement `task`.

    progress_callback(msg) is called every 30s while running.
    done_callback(success, summary) is called when finished.
    """

    def _worker():
        start = time.monotonic()
        stop_flag = threading.Event()

        def _ticker():
            while not stop_flag.wait(30):
                elapsed = int(time.monotonic() - start)
                progress_callback(f"⏳ Claude Code 執行中... ({elapsed}s)")

        ticker = threading.Thread(target=_ticker, daemon=True)
        ticker.start()

        try:
            result = subprocess.run(
                [
                    CLAUDE_BIN,
                    "-p",
                    _SYSTEM_PROMPT + "任務：" + task,
                    "--allowedTools",
                    "Read,Edit,Write,Bash",
                ],
                cwd=HERMES_DIR,
                capture_output=True,
                text=True,
                timeout=TIMEOUT_SECONDS,
            )
            stop_flag.set()
            output = (result.stdout or result.stderr or "(無輸出)").strip()
            done_callback(result.returncode == 0, output)
        except subprocess.TimeoutExpired:
            stop_flag.set()
            done_callback(False, f"❌ 超時（{TIMEOUT_SECONDS // 60} 分鐘），任務中止")
        except FileNotFoundError:
            stop_flag.set()
            done_callback(False, f"❌ 找不到 claude CLI：{CLAUDE_BIN}")
        except Exception as e:
            stop_flag.set()
            done_callback(False, f"❌ 執行錯誤：{e}")

    threading.Thread(target=_worker, daemon=True).start()


def restart_bot() -> None:
    """Restart the Telegram bot launchd service to load new code."""
    subprocess.Popen(
        ["launchctl", "kickstart", "-k", "gui/501/com.hermes.telegrambot"]
    )
