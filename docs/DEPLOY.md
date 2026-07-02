# 完整部署指南

## 架構前提

Jarvis 由多個 macOS 常駐服務組成(launchd),互相以 localhost HTTP 溝通:

| 埠 | 服務 | 檔案 | 必要性 |
|---|---|---|---|
| 8809 | 記憶/財務中樞 | `brain/scripts/hermes_memory_endpoint.py` | ✅ 核心 |
| 8811 | 控制台 dashboard | `brain/dashboard/hermes_dashboard.py` | ✅ 核心 |
| 8808 | LLM 金鑰輪換代理 | `brain/scripts/llm_proxy.py` | ✅ 核心 |
| 8769 | hermes-life MCP 工具 | `brain/scripts/hermes_life_mcp.py` | ✅ 核心 |
| 8642 | hermes-agent 大腦 | 外部專案(見下) | ✅ 核心 |
| 8643 | 語音橋接 | `brain/scripts/voice_brain_bridge.py` | 🤖 有機器人才要 |
| 8806 | MLX Whisper ASR | `brain/scripts/mlx_asr_server.py` | 🤖 |
| 8807 | 聲紋識別 | `brain/scripts/voiceprint_server.py` | 🤖 |
| 8000 | xiaozhi 語音伺服器 | Docker | 🤖 |

## Step 1 — Python 環境

```bash
python3 -m venv .venv
.venv/bin/pip install -r brain/requirements-embedded.txt   # fastapi uvicorn pyyaml requests 等
# ASR 額外(Apple Silicon): .venv/bin/pip install mlx-whisper opencc
# 聲紋額外: .venv/bin/pip install resemblyzer
```

## Step 2 — 設定檔

```bash
cp .env.example .env                       # LLM 金鑰
cd brain/config
cp finance.example.json finance.json       # 你的收入/固定開銷/持股/目標
cp telegram.example.json telegram.json     # bot token + 你的 user id
cp expenses.example.json expenses.json     # 可清空從零開始
```

## Step 3 — 大腦(hermes-agent)

大腦是獨立的 agent runtime(OpenAI 相容 API + agent loop + MCP client)。
任何提供 **OpenAI 相容 API + MCP 工具掛載**的 agent 框架皆可替換;設定要點:

- 對外開 `:8642` /v1/chat/completions
- 掛 MCP server:`http://127.0.0.1:8769`(hermes-life,streamable-http)
- system prompt 掛 context provider:`http://127.0.0.1:8809/hermes_memory`(事實+相處守則+即時時間)

## Step 4 — launchd 常駐

`brain/launchd/*.plist` 為範本:把裡面的絕對路徑換成你的,然後:

```bash
cp brain/launchd/com.hermes.*.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.hermes.*.plist
```

夜間自我進化排程:`com.hermes.selfreflect`(03:00)與 `com.hermes.selfreview`(03:15)。

## Step 5 — 語音機器人(選配)

1. 硬體:M5Stack CoreS3 + StackChan 套件,刷 xiaozhi ESP32 韌體(喚醒詞引擎在裝置上)
2. 伺服器:clone [xinnan-tech/xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server),把 `voice-server/patches/` 以 docker-compose volume 掛進容器(見 `voice-server/docker-compose.yml`)
3. `cp voice-server/config.example.yaml data/.config.yaml`,填你 Mac 的區網 IP
4. `docker compose up -d`,裝置韌體填 `ws://<你的IP>:8000/xiaozhi/v1/`

> ⚠️ 新增語音工具後必須 recreate 容器(volume 掛載的 patch 是 import 時載入)。

## Step 6 — 驗收

```bash
curl http://127.0.0.1:8809/health          # ok
curl "http://127.0.0.1:8809/finance_summary?q=今天花多少"
open http://localhost:8811                 # JARVIS HUD
```

## 常見坑

- **macOS 沒有 `timeout` 指令**:腳本已避免,自己 debug 時注意
- **Whisper 首次啟動會從 HuggingFace 拉 1.5GB 模型**:之後 launchd 已設 `HF_HUB_OFFLINE=1` 用快取
- **裝置連不上**:檢查 Mac 區網 IP 是否變動(建議 DHCP 保留)
