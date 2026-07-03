# 🤖 Module: StackChan Control

**Purpose**: Physical movement and expression control.
**Logic**: Intent -> Command Translation -> Hardware Execution.

**狀態**: ✅ 已實作（Hermes 端）。詳細規格見 `specs/phase_04_embodied_soul/`。

## 實作位置
- `modules/embodied/mqtt_bridge.py` — MQTT 連線層
- `modules/embodied/command_mapper.py` — Intent -> Topic/Payload 對照表
- `modules/embodied/sensory_listener.py` — 感測事件接收與記錄
- `modules/embodied/audio_bridge.py` — 語音對話 HTTP 端點
- `modules/embodied/offline_sync.py` — 離線事件補登
- `modules/embodied/skills/` — 可插拔功能（天氣播報、問候反應等）
- `scripts/embodied_daemon.py` — 常駐主程式

## 啟動方式
```bash
cd /Users/USERNAME/Hermes_Brain
./.venv/bin/python -m scripts.embodied_daemon
```

## 設定檔
`config/embodied.json` — MQTT broker 位址、audio bridge port、TTS 聲音、
所在地座標、啟用的 skills 清單。
