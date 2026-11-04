#!/bin/bash
# =============================================================================
# Hermes Arrival — StackChan 到貨後（或任何時候）一鍵把整套帶起來並驗收
# =============================================================================
# 安裝/載入所有常駐服務 → 確認 mosquitto → gateway preflight → 全系統健檢
# → 印出韌體配對資訊（WebSocket 網址 + token）。可重複執行（idempotent）。
#
#   ./scripts/arrival.sh
# =============================================================================
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PY="$ROOT/.venv/bin/python"
LA="$HOME/Library/LaunchAgents"

bold() { printf "\033[1m%s\033[0m\n" "$1"; }
dim()  { printf "\033[2m%s\033[0m\n" "$1"; }

bold "▶ 1/5 安裝 / 載入常駐服務 (launchd)"
for plist in com.hermes.llmproxy com.hermes.stackchan com.hermes.voiceloop \
             com.hermes.telegrambot com.hermes.reminderdaemon com.hermes.dailybriefing; do
    src="$ROOT/launchd/$plist.plist"
    [ -f "$src" ] || src="$LA/$plist.plist"   # 有些原本就裝在 LA
    if [ -f "$ROOT/launchd/$plist.plist" ]; then
        cp "$ROOT/launchd/$plist.plist" "$LA/" 2>/dev/null || true
    fi
    if [ -f "$LA/$plist.plist" ]; then
        launchctl unload "$LA/$plist.plist" 2>/dev/null || true
        launchctl load "$LA/$plist.plist" 2>/dev/null && echo "  ✓ $plist" || echo "  ⚠ $plist 載入失敗"
    else
        echo "  ⚠ 找不到 $plist.plist"
    fi
done

bold "▶ 2/5 確認 MQTT broker (mosquitto)"
if pgrep -f mosquitto >/dev/null; then echo "  ✓ 已執行"; else
    brew services start mosquitto 2>/dev/null && echo "  ✓ 已啟動" || echo "  ⚠ 無法啟動（僅舊 MQTT 韌體需要）"
fi

bold "▶ 3/5 StackChan gateway 連線埠"
sleep 2
for pair in "WS:$("$PY" -c "import json;print(json.load(open('config/stackchan.json'))['ws_port'])")" \
            "MCP:$("$PY" -c "import json;print(json.load(open('config/stackchan.json'))['mcp_http_port'])")" \
            "Voice:$("$PY" -c "import json;print(json.load(open('config/stackchan.json'))['voice_loop_port'])")"; do
    name="${pair%%:*}"; port="${pair##*:}"
    if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
        echo "  ✓ $name :$port 監聽中"
    else
        echo "  ⚠ $name :$port 沒在監聽"
    fi
done

bold "▶ 4/5 全系統健康檢查"
"$PY" -m scripts.healthcheck 2>/dev/null

bold "▶ 5/5 韌體配對資訊（燒錄 / 設定裝置時用）"
LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo 127.0.0.1)"
TOKEN="$("$PY" -c "import json;print(json.load(open('config/stackchan.json'))['token'])")"
WS_PORT="$("$PY" -c "import json;print(json.load(open('config/stackchan.json'))['ws_port'])")"
echo ""
dim   "  把 StackChan 接上跟 Mac 同一個 WiFi，並在韌體/配對畫面填入："
echo  "    WebSocket Gateway URL : ws://${LAN_IP}:${WS_PORT}"
echo  "    Bearer Token          : ${TOKEN}"
dim   "  （建議在路由器把 Mac 的 IP 設成 DHCP 保留，避免 IP 變動）"
echo ""
dim   "  裝置連上後，再跑一次 ./scripts/arrival.sh，「StackChan 實體裝置」會變綠燈。"
echo ""
