# Hermes 架構（2026-06-18 統一版）

> 一個 Hermes，三個介面，共用記憶 / 金鑰 / 人格。

## 全貌

```
                         ┌──────────────────────────────┐
   在家講話 ────────────▶│  StackChan (CoreS3, xiaozhi)  │
                         └───────────────┬──────────────┘
                            WS :8770 (control)│ HTTP :8771 (photo)
                                             ▼
                        ┌─────────────────────────────────┐
                        │  stackchan-mcp gateway          │  ← launchd
                        │  MCP (HTTP) :8767  /  bearer     │
                        └──────┬──────────────────┬───────┘
        裝置主動語音 hook       │                  │  MCP tools (35)
        (Ogg/Opus) :8801       ▼                  ▼
                  ┌────────────────────┐   ┌──────────────────────┐
                  │ voice_loop :8801   │   │  hermes-agent CLI    │ ← `hermes`
                  │ 轉文字→大腦→say()   │   │  (Nous, gemma model) │
                  └─────────┬──────────┘   └──────────┬───────────┘
                            │                          │
   Telegram ──────────┐     │  voice_brain             │ mcp_servers.stackchan
                      ▼     ▼  (共用意圖/記憶)           │
              ┌────────────────────────────┐            │
              │  Hermes_Brain 大腦 (3.9)    │            │
              │  gemini_client / 22 skills │            │
              └─────────────┬──────────────┘            │
                            │                            │
          ┌─────────────────┼────────────────────────────┘
          ▼                 ▼
  ┌───────────────┐  ┌──────────────────────────────────┐
  │ LLM 金鑰輪換   │  │ 共用記憶 ~/.hermes/memories/      │
  │ proxy :8808   │  │   USER.md / MEMORY.md (flock)    │
  │ 7×Gemini key  │  │  + Hermes_Brain 向量 DB          │
  └───────┬───────┘  └──────────────────────────────────┘
          ▼
   generativelanguage.googleapis.com
```

## 常駐服務（launchd，全部 KeepAlive + RunAtLoad）

| Label | Port | 作用 |
| :-- | :-- | :-- |
| `com.hermes.llmproxy` | 8808 | Gemini 金鑰輪換代理（三端共用） |
| `com.hermes.stackchan` | 8770/8771/8767 | 機器人 gateway（裝置 WS / 拍照 / MCP） |
| `com.hermes.voiceloop` | 8801 | 在家免持語音迴圈 |
| `com.hermes.telegrambot` | — | Telegram 長輪詢 |
| `com.hermes.reminderdaemon` | — | 提醒排程 |

外加 `mosquitto`（MQTT，僅 reminder/legacy 用）。

## 三端如何「同一個」

- **金鑰**：全部 LLM 流量走 `:8808` 代理 → 共用 `config/keys.json` 輪換狀態。
  hermes-agent 的 `~/.hermes/config.yaml` `base_url` 已指到代理的 `/v1beta/openai`。
- **記憶**：`modules/memory/hermes_agent_bridge.py` 讀寫 `~/.hermes/memories/`，
  與 hermes-agent 原生記憶同檔；Telegram / 語音 / CLI 三端共享。
- **人格**：Hermes_Brain 用 `gemini_client.SYSTEM_PROMPT`；hermes-agent 用
  `agent.system_prompt`（已設為同一個「Hermes 助理」人格）。

## 設定檔

- `config/stackchan.json` — gateway token / 各 port。
- `config/keys.json` — 7 把 Gemini key + 輪換狀態。
- `config/embodied.json` / `config/telegram.json` — 既有。
- `~/.hermes/config.yaml` + `~/.hermes/.env` — hermes-agent（base_url、token、persona）。

## Telegram 超能力

- **`/agent <任務>`** — 把需要動手的任務丟給完整 hermes-agent 深度代理
  （寫程式、查資料、操作電腦），非同步執行、完成回報。安全：預設不自動跑指令，
  要全自主在 `config/stackchan.json` 設 `agent_yolo: true`（= 遠端 RCE，慎開）。
- **傳照片** → Gemini Vision 看圖回答（同一條視覺管道日後給機器人 `take_photo` 用）。
- **傳短語音 (<60s)** → 當對話；長語音/音訊檔 → Plaud 會議逐字稿＋摘要＋待辦。
- **每天 08:00 主動簡報** — 提醒/食材/聯絡人/預算/今日思考，推到 Telegram
  （`scripts/daily_briefing.py`，`--dry-run` 可測）。

## 韌性 / 三種真實故障的處理

| 情況 | 會發生什麼 | 怎麼解 |
| :-- | :-- | :-- |
| **Mac 關機/重開** | 全部服務隨機停 | 全服務 `RunAtLoad` 開機自動回來；Telegram offset 持久化（`presence.py`）→ 重開不漏不重；偵測離線時長，回來時主動說「我離線了 X，現在回來了」 |
| **StackChan 不在旁邊** | 機器人指令無對象 | `notify.speak_if_present()`：裝置在線才唸，不在就安靜略過；提醒/簡報/警示**文字一律照送 Telegram**（人在外面也收得到） |
| **Gemini 連不上** | 大腦無法回覆 | proxy 5xx 退避重試 + 金鑰輪換；全掛時 → 本機 **Ollama** 後備（有抓模型就自動用）；再不行 → Telegram 回友善訊息並把問題**排進佇列**（`pending_queue.py`），大腦恢復後**自動補回覆**，絕不靜默丟訊息 |

後備大腦（已啟用）：本機 **Ollama `qwen2.5:7b`**（走 M3 Metal GPU）。後備順序
= Gemini → n8n webhook（若設）→ Ollama（本機/遠端）→ 友善訊息+排隊。
- 換/接遠端模型：`config/stackchan.json` 設 `ollama_url`（例 Mac Mini 的 Tailscale IP），
  或用 `scripts/connect_remote_brain.sh <url>` / `scripts/connect_n8n_brain.sh <webhook>`。
- Ollama 由你的 Ollama.app 管理，已加入開機登入項目 → 重開機後備也活著。
健檢的「韌性 / 容錯」區塊會顯示佇列深度、心跳、離線後備狀態。

## 常用指令

```bash
./scripts/arrival.sh                     # 一鍵帶起 + 健檢 + 配對資訊
./.venv/bin/python -m scripts.healthcheck # 全系統健康檢查
curl -s localhost:8808/admin/keys         # 看金鑰池狀態
hermes -z "..."                           # 用統一大腦（含機器人工具）
hermes mcp test stackchan                 # 測機器人工具連線
```
