#!/bin/bash
# Start the StackChan MCP gateway (xiaozhi-esp32 firmware bridge).
#
# 這是「實體機器人」的橋接層：
#   - 裝置 (CoreS3 / xiaozhi 韌體) 透過 WebSocket :8765 連進來
#   - 拍照透過 HTTP :8766 上傳
#   - 大腦 (hermes-agent) 透過 Streamable HTTP MCP :8767/mcp 取得機器人工具
#   - 裝置主動觸發的語音 (喚醒詞/按鍵) 會 POST 到 Hermes_Brain 既有的
#     audio bridge (:8800/voice)，流進同一個大腦
#
# LAN IP 動態偵測，避免 DHCP 換 IP 後設定失效。
# Usage: ./scripts/start_stackchan_gateway.sh
set -euo pipefail
cd "$(dirname "$0")/.."

CFG="config/stackchan.json"
PY="./.venv/bin/python"

read_cfg() { "$PY" -c "import json,sys;print(json.load(open('$CFG')).get('$1',''))"; }

TOKEN="$(read_cfg token)"
WS_PORT="$(read_cfg ws_port)"
CAPTURE_PORT="$(read_cfg capture_port)"
MCP_HTTP_HOST="$(read_cfg mcp_http_host)"
MCP_HTTP_PORT="$(read_cfg mcp_http_port)"
AUDIO_PATH="$(read_cfg audio_hook_path)"
AUDIO_PORT="$(read_cfg voice_loop_port)"

# 動態偵測本機 LAN IP（en0 優先，否則 en1）
LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo 127.0.0.1)"

# 讓 opuslib 找得到 Homebrew 的 libopus（TTS 推音到裝置要 Opus 編碼）
export DYLD_FALLBACK_LIBRARY_PATH="/opt/homebrew/lib:${DYLD_FALLBACK_LIBRARY_PATH:-}"

export STACKCHAN_TOKEN="$TOKEN"
export VISION_HOST="$LAN_IP"
export STACKCHAN_AUDIO_HOOK_URL="http://${LAN_IP}:${AUDIO_PORT}${AUDIO_PATH}"
export STACKCHAN_AUDIO_HOOK_TOKEN="$TOKEN"
export HOST="0.0.0.0"
export WS_PORT="$WS_PORT"
export CAPTURE_PORT="$CAPTURE_PORT"
export MCP_HTTP_HOST="$MCP_HTTP_HOST"
export MCP_HTTP_PORT="$MCP_HTTP_PORT"

echo "🤖 [StackChan Gateway] LAN_IP=$LAN_IP  WS=:$WS_PORT  MCP=http://$MCP_HTTP_HOST:$MCP_HTTP_PORT/mcp"
echo "    語音 hook → $STACKCHAN_AUDIO_HOOK_URL"

exec "$HOME/.local/bin/stackchan-mcp" serve --transport streamable-http
