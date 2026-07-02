#!/bin/bash
# 把 macOS 中文 TTS 引擎 (macsay) 裝進 stackchan-mcp gateway。
# 可重複執行；uv tool 重裝後再跑一次即可。
set -uo pipefail
PKG=$(/bin/ls -d "$HOME"/.local/share/uv/tools/stackchan-mcp/lib/python*/site-packages/stackchan_mcp 2>/dev/null | head -1)
[ -z "$PKG" ] && { echo "找不到 stackchan_mcp 套件"; exit 1; }
SRC="$(dirname "$0")/../firmware/stackchan_tts/mac_say.py"

echo "▶ 複製引擎 → $PKG/tts/mac_say.py"
cp "$SRC" "$PKG/tts/mac_say.py"

INIT="$PKG/tts/__init__.py"
if ! grep -q "_register_macsay" "$INIT"; then
  echo "▶ 在 __init__.py 註冊 macsay"
  cat >> "$INIT" <<'PY'


def _register_macsay() -> None:
    from .mac_say import register
    register()


_try_register(_register_macsay, "macsay")
PY
else
  echo "▶ __init__.py 已含 macsay 註冊，跳過"
fi
echo "✅ 完成。重啟 gateway 生效：launchctl kickstart -k gui/\$(id -u)/com.hermes.stackchan"
