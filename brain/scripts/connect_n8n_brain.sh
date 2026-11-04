#!/bin/bash
# =============================================================================
# 把 n8n 的 Webhook 接成 Hermes 的後備大腦（n8n 後面接你已設好的本地模型）
# =============================================================================
# 用法：
#   ./scripts/connect_n8n_brain.sh https://你的n8n/webhook/hermes-brain [Bearer token]
#   ./scripts/connect_n8n_brain.sh --clear
#
# n8n 工作流：Webhook(POST) → 你的本地模型節點 → Respond to Webhook
#   收到的 body 有 {"prompt": "使用者的話"}；
#   請讓 Respond 回傳純文字，或 JSON 含 text/response/output/answer 任一欄。
# =============================================================================
set -uo pipefail
cd "$(dirname "$0")/.."
PY="./.venv/bin/python"
CFG="config/stackchan.json"

if [ "${1:-}" = "--clear" ]; then
    "$PY" -c "import json;c=json.load(open('$CFG'));c.pop('n8n_brain_url',None);c.pop('n8n_brain_token',None);json.dump(c,open('$CFG','w'),indent=2)"
    echo "✅ 已取消 n8n 後備"
    exit 0
fi

URL="${1:-}"
TOKEN="${2:-}"
if [ -z "$URL" ]; then
    echo "用法: $0 <n8n webhook URL> [Bearer token]"
    exit 1
fi

echo "▶ 1/2 測試 webhook：$URL"
HDR=(-H "Content-Type: application/json")
[ -n "$TOKEN" ] && HDR+=(-H "Authorization: Bearer $TOKEN")
RESP=$(curl -s -m 60 "${HDR[@]}" -X POST "$URL" -d '{"prompt":"用繁體中文回一句話打招呼"}' 2>/dev/null)
if [ -z "$RESP" ]; then
    echo "❌ 沒有回應。檢查：n8n 工作流有沒有 active？Webhook 是不是 Production URL？有沒有 Respond to Webhook 節點？"
    exit 1
fi
echo "   ✓ 回應：$(echo "$RESP" | head -c 200)"

echo "▶ 2/2 寫入設定"
"$PY" -c "import json;c=json.load(open('$CFG'));c['n8n_brain_url']='$URL';${TOKEN:+c['n8n_brain_token']='$TOKEN';}json.dump(c,open('$CFG','w'),indent=2)"
echo "   ✓ config/stackchan.json -> n8n_brain_url 設好了"
echo ""
echo "✅ 完成！重啟讓服務吃到設定："
echo "   launchctl kickstart -k gui/\$(id -u)/com.hermes.telegrambot"
echo "之後 Gemini 掛掉時，會自動改打 n8n → 你的本地模型。"
