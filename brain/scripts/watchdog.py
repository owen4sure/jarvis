"""Hermes 外部監控（watchdog）。

每 2 分鐘檢查關鍵服務的埠是否還活著；掛了就 ①嘗試用 launchctl 重啟 ②發 Telegram 通知 Owen。
只在「狀態改變」時通知（up→down 發「掛了」、down→up 發「恢復了」），避免洗版。
純外部、不依賴任何被監控的服務，所以就算 8809/大腦整個掛掉，這個還是會通知你。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.parse
import urllib.request

UID = os.getuid()
STATE = os.path.expanduser("~/.hermes/.watchdog_state.json")
ENV = os.path.expanduser("~/.hermes/.env")
CHAT_ID = "0  # ← 換成你的 Telegram chat_id(或改讀 config/telegram.json)"

# (顯示名, 埠, launchd label 或 None=只通知不自動重啟)
SERVICES = [
    ("大腦 8642", 8642, "ai.hermes.gateway"),
    ("語音橋接 8643", 8643, "com.hermes.voicebridge"),
    ("LLM Proxy 8808", 8808, "com.hermes.llmproxy"),
    ("記憶財務 8809", 8809, "com.hermes.memoryendpoint"),
    ("聲紋 8807", 8807, "com.hermes.voiceprint"),
    ("語音辨識 8806", 8806, "com.hermes.mlxasr"),
    ("儀表板 8811", 8811, "com.hermes.dashboard"),
    ("生活 MCP 8769", 8769, "com.hermes.lifemcp"),
]
CONTAINER = "xiaozhi-esp32-server"


def _port_alive(port: int) -> bool:
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=3)
        s.close()
        return True
    except Exception:
        return False


def _container_alive() -> bool:
    try:
        out = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER],
                             capture_output=True, text=True, timeout=8)
        return out.stdout.strip() == "true"
    except Exception:
        return True  # docker 查不到就當它活著，別誤報


def _bot_token() -> str:
    try:
        for line in open(ENV):
            if line.startswith("TELEGRAM_BOT_TOKEN"):
                return line.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    return ""


def _tg(msg: str) -> None:
    tok = _bot_token()
    if not tok:
        return
    try:
        data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": msg}).encode()
        urllib.request.urlopen(
            urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage", data=data),
            timeout=8)
    except Exception:
        pass


def _restart(label: str) -> None:
    try:
        subprocess.run(["launchctl", "kickstart", "-k", f"gui/{UID}/{label}"],
                       capture_output=True, timeout=15)
    except Exception:
        pass


def _load_state() -> dict:
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def _save_state(st: dict) -> None:
    try:
        json.dump(st, open(STATE, "w"))
    except Exception:
        pass


def run_once() -> None:
    st = _load_state()
    checks = [(n, p, _port_alive(p), lbl) for n, p, lbl in SERVICES]
    checks.append(("StackChan 容器", 0, _container_alive(), "__container__"))
    for name, port, alive, lbl in checks:
        was = st.get(name, True)   # 預設先前是活的
        if not alive:
            # 掛了 → 嘗試重啟
            if lbl == "__container__":
                try:
                    subprocess.run(["docker", "restart", CONTAINER], capture_output=True, timeout=30)
                except Exception:
                    pass
            elif lbl:
                _restart(lbl)
            time.sleep(4)
            recovered = _container_alive() if lbl == "__container__" else _port_alive(port)
            if was:   # 狀態剛從 活→掛：通知一次
                _tg(f"⚠️ Hermes：{name} 掛了。" + ("已自動重啟" + ("成功✅" if recovered else "但還沒起來❌，可能要你看一下") if lbl else "（沒有自動重啟、要手動處理）"))
            st[name] = bool(recovered)
        else:
            if not was:   # 掛→活：通知恢復
                _tg(f"✅ Hermes：{name} 恢復正常了。")
            st[name] = True
    _save_state(st)


if __name__ == "__main__":
    run_once()
