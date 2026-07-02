# 擴充 Jarvis / Extending Jarvis

三種方式,由淺入深。/ Three ways, from zero-code to full control.

---

## 方式一:用講的(零程式碼)/ Just ask Jarvis

對機器人或 Telegram 說:

> 「幫我做一個記錄每天喝幾杯水的功能」

`build_feature` 會把需求交給 Claude Code CLI,自動完成:
1. 後端 API(`hermes_memory_endpoint.py` 新端點 + `config/` 資料檔)
2. 語音工具(掛進 MCP,所有管道即刻可用)
3. 控制台面板(`dashboard/index.html` + API 代理)
4. 冒煙測試 → 自動重啟上線;失敗自動還原並把錯誤餵回去自我修正(最多 3 輪)

Speak to the robot or Telegram: *"Build me a feature that tracks my daily water intake."* The `build_feature` pipeline hands it to Claude Code CLI — backend endpoint, voice tool, dashboard panel, wired and auto-deployed behind smoke-test/rollback gates.

## 方式二:加一個工具(10 行)/ Add a tool (10 lines)

所有管道(語音+Telegram)的工具都註冊在 **`brain/scripts/hermes_life_mcp.py`**(MCP server :8769)。加一次,全管道獲得:

```python
@mcp.tool()
def my_tool(arg: str) -> str:
    """工具描述寫清楚——agent 靠這段決定何時呼叫。
    Describe when to use this — the agent routes by this docstring."""
    r = _post(f"{MEM}/my_endpoint", {"arg": arg})
    return r.get("text") or "done"
```

重啟生效 / restart to load:
```bash
launchctl kickstart -k gui/$(id -u)/com.hermes.lifemcp
launchctl kickstart -k gui/$(id -u)/ai.hermes.gateway
```

## 方式三:完整功能(後端+面板)/ Full feature (backend + panel)

1. **後端**:在 `brain/scripts/hermes_memory_endpoint.py` 加 FastAPI 端點,資料存 `brain/config/*.json`
2. **工具**:同方式二,包一層 MCP 工具打你的新端點
3. **面板**:`brain/dashboard/index.html` 加一個 panel(跟著現有的 CSS 變數風格),`brain/dashboard/hermes_dashboard.py` 加 `/api` 代理
4. **確定性答案**(選配):高頻問題加進 `_focused_finance_answer` 樣式的分支——程式算好完整句子,模型照唸,正確率 100%

## 加主動行為 / Add proactive behaviors

`brain/scripts/proactive_engine.py` — 寫一個 checker 函式加進 `CHECKERS`,回傳要推播的訊息即可(引擎管去重、安靜時段、頻率上限):

```python
def check_something(now):
    if condition:
        return [{"key": "unique_key", "urgency": "general",
                 "tg": "Telegram 訊息", "voice": "機器人要講的話"}]
    return []
```

## 加夜間自我進化的觀察角度 / Extend nightly evolution

- 人格面:`brain/scripts/self_reflect.py` 的 prompt 定義它從對話中學什麼
- 能力面:`brain/scripts/self_review.py` 定義它怎麼發現缺口、怎麼提案
