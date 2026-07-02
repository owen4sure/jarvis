"""
Hermes 全系統健康檢查
=====================
一個指令確認「三者同一個大腦」整套是否就緒：
    ./.venv/bin/python -m scripts.healthcheck

檢查項目：MQTT broker、LLM 金鑰輪換代理、金鑰池、StackChan gateway（含裝置是否
連線）、語音迴圈、Telegram/Reminder 常駐、共用記憶、hermes-agent。
回傳碼 0 = 全部 OK（或僅缺實體裝置）；非 0 = 有阻斷性問題。
"""
import json
import os
import subprocess
import sys

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

GREEN, RED, YEL, DIM, RST = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
OK, FAIL, WARN = f"{GREEN}✓{RST}", f"{RED}✗{RST}", f"{YEL}⚠{RST}"

results = []  # (level, name, detail)  level: ok/warn/fail


def add(level, name, detail=""):
    results.append((level, name, detail))
    icon = {"ok": OK, "warn": WARN, "fail": FAIL}[level]
    print(f"  {icon} {name:<28} {DIM}{detail}{RST}")


def _http_ok(url, timeout=4, headers=None):
    try:
        r = requests.get(url, timeout=timeout, headers=headers or {})
        return r.status_code < 500, r
    except Exception as e:
        return False, e


def check_mosquitto():
    out = subprocess.run(["pgrep", "-f", "mosquitto"], capture_output=True, text=True)
    if out.stdout.strip():
        add("ok", "MQTT broker (mosquitto)", f"pid {out.stdout.split()[0]}")
    else:
        add("warn", "MQTT broker (mosquitto)", "未執行（僅舊 MQTT 韌體需要）")


def check_proxy():
    ok, r = _http_ok("http://127.0.0.1:8808/healthz")
    if ok:
        add("ok", "LLM 金鑰輪換代理 :8808", "healthz ok")
    else:
        add("fail", "LLM 金鑰輪換代理 :8808", f"無回應 ({r})")


def check_keys():
    ok, r = _http_ok("http://127.0.0.1:8808/admin/keys")
    if not ok:
        add("fail", "Gemini 金鑰池", "無法讀取")
        return
    data = r.json()
    active = sum(1 for k in data["keys"] if k["status"] == "active")
    total = data["total"]
    lvl = "ok" if active > 0 else "fail"
    add(lvl, "Gemini 金鑰池", f"{active}/{total} active")


def check_gemini_call():
    try:
        r = requests.post(
            "http://127.0.0.1:8808/v1beta/openai/chat/completions",
            json={"model": "gemini-2.5-flash-lite",
                  "messages": [{"role": "user", "content": "ping, reply PONG"}]},
            timeout=30,
        )
        txt = r.json()["choices"][0]["message"]["content"]
        add("ok", "Gemini 端到端 (經代理)", f"reply: {txt[:20].strip()}")
    except Exception as e:
        add("fail", "Gemini 端到端 (經代理)", str(e)[:50])


def check_gateway():
    cfg = json.load(open(os.path.join(ROOT, "config", "stackchan.json")))
    token = cfg.get("token", "")
    base = f"http://{cfg.get('mcp_http_host','127.0.0.1')}:{cfg.get('mcp_http_port',8767)}/mcp"
    try:
        r = requests.post(base, headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                            "clientInfo": {"name": "healthcheck", "version": "1"}}},
            timeout=6)
        if r.status_code == 200:
            add("ok", "StackChan gateway :8767", "MCP 在線")
        else:
            add("fail", "StackChan gateway :8767", f"HTTP {r.status_code}")
    except Exception as e:
        add("fail", "StackChan gateway :8767", str(e)[:50])

    # 裝置是否連線（沒到貨時為 warn，不算 fail）
    try:
        from modules.embodied.stackchan_mcp_client import StackChanClient
        st = StackChanClient().get_status()
        txt = json.dumps(st.get("result", st))
        connected = '"connected": true' in txt or '"connected":true' in txt
        if connected:
            add("ok", "StackChan 實體裝置", "已連線 🤖")
        else:
            add("warn", "StackChan 實體裝置", "尚未連線（到貨/燒錄後會自動接上）")
    except Exception as e:
        add("warn", "StackChan 實體裝置", str(e)[:50])


def check_voice_loop():
    ok, r = _http_ok("http://127.0.0.1:8801/health")
    add("ok" if ok else "fail", "語音迴圈 :8801", "health ok" if ok else f"無回應 ({r})")


def check_launchd(label, name, optional=False):
    out = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    running = any(label in line and not line.split()[0].startswith("-")
                  for line in out.stdout.splitlines() if label in line)
    present = label in out.stdout
    if running or present:
        add("ok", name, "launchd 已載入")
    else:
        add("warn" if optional else "fail", name, "未載入")


def check_memory():
    up = os.path.expanduser("~/.hermes/memories/USER.md")
    mm = os.path.expanduser("~/.hermes/memories/MEMORY.md")
    ok = os.path.exists(up) and os.path.exists(mm)
    detail = f"USER.md {os.path.getsize(up)}B / MEMORY.md {os.path.getsize(mm)}B" if ok else "缺檔"
    add("ok" if ok else "fail", "共用記憶 (三端)", detail)


def check_resilience():
    # 待回覆佇列深度（大腦曾連不上而積壓的訊息）
    try:
        from modules.remote import pending_queue, presence
        n = len(pending_queue.list_pending())
        add("ok" if n == 0 else "warn", "待回覆佇列",
            "空（沒有積壓）" if n == 0 else f"{n} 則待大腦恢復後補回覆")
        down = presence.downtime_seconds()
        hb = presence.read()
        if hb:
            add("ok", "在線心跳", f"最後活著 {presence.human_duration(down)}前")
        else:
            add("warn", "在線心跳", "尚無紀錄（bot 還沒寫過）")
    except Exception as e:
        add("warn", "韌性狀態", str(e)[:50])
    # 離線後備（本機 Ollama 模型）
    try:
        from modules.embodied.llm_fallback import ollama_model
        m = ollama_model(refresh=True)
        if m:
            add("ok", "離線後備大腦", f"Ollama: {m}")
        else:
            add("warn", "離線後備大腦", "未啟用（可 `ollama pull qwen2.5:3b` 開啟離線回應）")
    except Exception as e:
        add("warn", "離線後備大腦", str(e)[:50])


def check_hermes_agent():
    try:
        r = subprocess.run(["/Users/chenyouwei/.local/bin/hermes", "-z", "回一個字：好"],
                           capture_output=True, text=True, timeout=90)
        reply = (r.stdout or "").strip().splitlines()[-1] if r.stdout.strip() else ""
        bad = ("API call failed", "failed after", "Traceback", "Error:")
        if reply and not any(b in reply for b in bad):
            add("ok", "hermes-agent 大腦", f"reply: {reply[:20]}")
        else:
            add("fail", "hermes-agent 大腦", (reply or r.stderr or "no output")[:50])
    except Exception as e:
        add("fail", "hermes-agent 大腦", str(e)[:50])


def main():
    os.chdir(ROOT)
    print(f"\n{'='*54}\n  🩺 Hermes 全系統健康檢查\n{'='*54}")
    print(f"\n{DIM}— 核心 LLM —{RST}")
    check_proxy(); check_keys(); check_gemini_call()
    print(f"\n{DIM}— 機器人 —{RST}")
    check_gateway(); check_voice_loop(); check_mosquitto()
    print(f"\n{DIM}— 常駐服務 (launchd) —{RST}")
    check_launchd("com.hermes.llmproxy", "  llmproxy")
    check_launchd("com.hermes.stackchan", "  stackchan gateway")
    check_launchd("com.hermes.voiceloop", "  voiceloop")
    check_launchd("com.hermes.telegrambot", "  telegrambot")
    check_launchd("com.hermes.reminderdaemon", "  reminderdaemon", optional=True)
    print(f"\n{DIM}— 記憶與大腦 —{RST}")
    check_memory(); check_hermes_agent()
    print(f"\n{DIM}— 韌性 / 容錯 —{RST}")
    check_resilience()

    fails = [r for r in results if r[0] == "fail"]
    warns = [r for r in results if r[0] == "warn"]
    print(f"\n{'='*54}")
    if not fails:
        print(f"  {GREEN}全部就緒{RST} — {len(warns)} 個提醒（多半是實體裝置尚未到貨）")
        print(f"{'='*54}\n")
        return 0
    print(f"  {RED}{len(fails)} 個阻斷性問題{RST} / {len(warns)} 個提醒")
    for _, n, d in fails:
        print(f"    {RED}✗{RST} {n.strip()} — {d}")
    print(f"{'='*54}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
