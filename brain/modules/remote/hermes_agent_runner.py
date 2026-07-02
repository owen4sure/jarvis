"""
hermes_agent_runner — 從 Telegram/語音 呼叫「完整 hermes-agent 深度代理」
============================================================================
Hermes_Brain 的輕量大腦（gemini_client）負責日常對話與技能；遇到需要
「真的動手」的任務（寫程式、跑指令、上網研究、操作電腦）就交給 NousResearch
hermes-agent——它有完整工具、會自己多步驟執行。兩者共用同一份記憶與金鑰，
所以是同一個 Hermes 的「深度模式」。

用 `hermes -z <task>` 一次性執行，擷取最終輸出回傳。
"""
import json
import os
import subprocess

HERMES_BIN = os.path.expanduser("~/.local/bin/hermes")
DEFAULT_TIMEOUT = 600  # 深度任務可能要好幾分鐘
_CFG = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "config", "stackchan.json")


def _yolo_enabled() -> bool:
    """是否允許深度代理自主執行任意指令（RCE 風險，預設關閉）。
    要打開：在 config/stackchan.json 設 "agent_yolo": true —— 等於允許
    透過 Telegram 在這台 Mac 上跑任意指令，請確認你了解風險再開。"""
    try:
        with open(_CFG, encoding="utf-8") as f:
            return bool(json.load(f).get("agent_yolo", False))
    except Exception:
        return False


def run_agent_task(task: str, timeout: int = DEFAULT_TIMEOUT, yolo: bool = None) -> str:
    """執行一個 hermes-agent 任務，回傳輸出文字（已截斷到適合 Telegram）。
    yolo=None 時依設定檔（預設關閉，安全）。"""
    if not task or not task.strip():
        return "請說明要交給深度代理做什麼任務。"
    if not os.path.exists(HERMES_BIN):
        return f"⚠️ 找不到 hermes 指令（{HERMES_BIN}）。"

    if yolo is None:
        yolo = _yolo_enabled()
    cmd = [HERMES_BIN, "-z", task.strip()]
    if yolo:
        cmd.insert(1, "--yolo")  # 自主執行任意工具（僅在 config 明確開啟時）

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            cwd=os.path.expanduser("~"),
        )
    except subprocess.TimeoutExpired:
        return f"⏱️ 深度代理執行超過 {timeout//60} 分鐘仍未完成，已中止。任務可拆小一點再試。"
    except Exception as e:
        print(f"⚠️ [agent_runner] {e}")
        return "深度代理這次沒跑成功，把任務說得更具體一點再試一次好嗎 🙏"

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if not out:
        return f"（深度代理沒有輸出）{(' / ' + err[-300:]) if err else ''}"

    # Telegram 單則訊息上限 ~4096，留點餘裕
    if len(out) > 3500:
        out = out[:1700] + "\n\n…（中間省略）…\n\n" + out[-1700:]
    return out
