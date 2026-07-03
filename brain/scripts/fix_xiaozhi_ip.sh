#!/bin/bash
# 開機/換網路時自動把 xiaozhi config 的 IP(WS+vision)更新成 Mac 當前 LAN IP，
# 避免換網路(熱點↔家WiFi)後裝置連不上/拍照POST到舊IP失敗。
IP=$(ifconfig 2>/dev/null | grep "inet " | grep -v 127.0.0.1 | grep -vE "169\.254" | awk '{print $2}' | head -1)
CFG=/Users/USERNAME/xiaozhi-server/data/.config.yaml
[ -z "$IP" ] && exit 0
CUR=$(grep -oE 'vision_explain: http://[0-9.]+' "$CFG" | grep -oE '[0-9.]+$')
if [ "$CUR" != "$IP" ]; then
  sed -i '' -E "s#http://[0-9.]+:8003#http://$IP:8003#g; s#ws://[0-9.]+:8000#ws://$IP:8000#g" "$CFG"
  echo "$(date) IP $CUR→$IP, 重啟xiaozhi" >> /Users/USERNAME/Hermes_Brain/memory/logs/ip_fix.log
  docker restart xiaozhi-esp32-server >/dev/null 2>&1
fi
