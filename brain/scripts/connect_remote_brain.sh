#!/bin/bash
# =============================================================================
# 把遠端本地模型（例如公司 Mac Mini 上的 ollama）接成 Hermes 的後備大腦
# =============================================================================
# 用法：
#   ./scripts/connect_remote_brain.sh http://<MacMini位址>:11434
#   ./scripts/connect_remote_brain.sh --clear          # 取消，改回本機
#
# 接好後：Gemini 掛掉時，Telegram/語音/hermes-agent 會自動改用遠端模型回覆。
# =============================================================================
set -uo pipefail
cd "$(dirname "$0")/.."
PY="./.venv/bin/python"
CFG="config/stackchan.json"

if [ "${1:-}" = "--clear" ]; then
    "$PY" -c "import json;c=json.load(open('$CFG'));c.pop('ollama_url',None);json.dump(c,open('$CFG','w'),indent=2)"
    echo "✅ 已取消遠端後備，改回本機 (http://localhost:11434)"
    exit 0
fi

URL="${1:-}"
if [ -z "$URL" ]; then
    echo "用法: $0 http://<位址>:11434"
    echo "  例: $0 http://100.101.102.103:11434   （Tailscale IP）"
    exit 1
fi
URL="${URL%/}"

echo "▶ 1/3 測試連線到 $URL ..."
MODELS=$(curl -s -m 8 "$URL/api/tags" 2>/dev/null)
if [ -z "$MODELS" ]; then
    echo "❌ 連不到。檢查清單："
    echo "   - Mac Mini 上 ollama 有設 OLLAMA_HOST=0.0.0.0 嗎？（預設只聽 localhost）"
    echo "   - 兩台在同一個 Tailscale/區網嗎？位址對嗎？"
    echo "   - Mac Mini 防火牆有擋 11434 嗎？"
    exit 1
fi
FIRST_MODEL=$(echo "$MODELS" | "$PY" -c "import sys,json;m=json.load(sys.stdin).get('models',[]);print(m[0]['name'] if m else '')" 2>/dev/null)
if [ -z "$FIRST_MODEL" ]; then
    echo "⚠️ 連到了，但對方沒有任何模型。請在 Mac Mini 上 ollama pull 一個模型。"
    exit 1
fi
echo "   ✓ 連到了，遠端模型：$FIRST_MODEL"

echo "▶ 2/3 寫入設定 ..."
"$PY" -c "import json;c=json.load(open('$CFG'));c['ollama_url']='$URL';json.dump(c,open('$CFG','w'),indent=2)"
echo "   ✓ config/stackchan.json -> ollama_url=$URL"

echo "▶ 3/3 實測一句（走遠端模型）..."
REPLY=$(curl -s -m 60 "$URL/v1/chat/completions" -H "Content-Type: application/json" \
    -d "{\"model\":\"$FIRST_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"用繁體中文回一句話打招呼\"}],\"stream\":false}" \
    | "$PY" -c "import sys,json;d=json.load(sys.stdin);print(d.get('choices',[{}])[0].get('message',{}).get('content','(無回應)'))" 2>/dev/null)
echo "   遠端回覆：$REPLY"
echo ""
echo "✅ 接好了！重啟服務讓 proxy 也吃到新設定："
echo "   launchctl kickstart -k gui/\$(id -u)/com.hermes.llmproxy"
echo "   launchctl kickstart -k gui/\$(id -u)/com.hermes.telegrambot"
echo "之後 Gemini 掛掉時，會自動改用這台遠端模型。"
