# StackChan <-> Hermes Firmware Integration Guide

> ⚠️ **LEGACY（2026-06-18 起不建議）**：這是「自寫 Arduino/PlatformIO 韌體 + MQTT」
> 的舊方案。最新官方 kit (CoreS3) 出廠是 xiaozhi 韌體，已改走
> **xiaozhi + stackchan-mcp** 路線——請看 `WHEN_HARDWARE_ARRIVES.md`。
> 這份僅在你日後改用可自燒的 Core2/Fire 時才需要。

這份指南是「硬體到貨後」要做的事。Hermes 端（Mac）已經 100% 完成並測試通過
（見 `WHEN_HARDWARE_ARRIVES.md`）。這裡只處理 ESP32 firmware 端。

## 0. 前置確認
你目前 StackChan 跑的是 **M5Stack 官方 stack-chan (Arduino/PlatformIO, C++)**。
到貨後請先：
1. 找到你實際使用的 stack-chan 原始碼 repo（PlatformIO 專案）。
2. 確認它的 `platformio.ini`、`src/main.cpp`（或 `.ino`），以及它怎麼呼叫
   表情 (avatar/expression)、舵機 (servo)、LED 的 API。
3. 確認開發板型號（Core2 / CoreS3 / Fire...），影響 PSRAM/LittleFS 分區。

這份 guide 不假設你的 repo 內部結構長什麼樣，而是提供一個**獨立的 MQTT 橋接
library** (`HermesBridge`)，你只需要在既有的 `setup()` / `loop()` 裡加幾行，
把 callback 接到你既有的表情/舵機/LED 函式上。

## 1. 安裝相依套件
在你的 stack-chan PlatformIO 專案的 `platformio.ini` 加入：
```ini
lib_deps =
    knolleary/PubSubClient @ ^2.8
    bblanchon/ArduinoJson @ ^6.21
```
（如果原專案已經有 ArduinoJson，確認版本是 6.x；HermesBridge 用的是 v6 API。）

## 2. 複製 HermesBridge library
把這個資料夾整個複製進你的 PlatformIO 專案的 `lib/` 目錄：
```
Hermes_Brain/firmware/HermesBridge/  ->  <your-stackchan-project>/lib/HermesBridge/
```
PlatformIO 會自動把 `lib/HermesBridge` 當成一個 library 編譯。

## 3. 設定連線參數
打開你專案的 `src/main.cpp`，在最上方加入：
```cpp
#include <HermesBridge.h>

const char* WIFI_SSID     = "你的WiFi名稱";
const char* WIFI_PASSWORD = "你的WiFi密碼";
const char* MQTT_HOST     = "192.168.1.102";  // 必須與 config/embodied.json -> mqtt.host 一致
const uint16_t MQTT_PORT  = 1883;
const char* TOPIC_PREFIX  = "hermes/stackchan";
const char* CLIENT_ID     = "stackchan";

HermesBridge hermes(WIFI_SSID, WIFI_PASSWORD, MQTT_HOST, MQTT_PORT, TOPIC_PREFIX, CLIENT_ID);
```

⚠️ **`MQTT_HOST` 是 Mac 在區網的 IP，DHCP 可能會變動。** 建議到貨前先到路由器
設定畫面，給 Mac 的 MAC 位址做「DHCP 固定 IP / 位址保留」，這樣這個值就永久
不用改。如果 IP 真的變了，只要同時改這裡和 `config/embodied.json` 的
`mqtt.host` 即可（兩邊必須一致）。

## 4. 在 setup() 中初始化並註冊指令處理
參考 `firmware/HermesBridge/examples/BasicBridge/BasicBridge.ino`，把
`hermes.begin()` 和四個 `hermes.onXxx(...)` callback 加進你的 `setup()`，
callback 裡呼叫你既有的表情/舵機/LED 函式即可。完整的指令對照表見
`specs/phase_04_embodied_soul/hardware_protocol_spec.md` 第 4 節。

| HermesBridge callback | 對應你既有 API（請替換成你專案實際的函式） |
| :--- | :--- |
| `onExpression` | `avatar.setExpression(...)` 或同等表情切換函式 |
| `onServo` | 舵機角度設定 / 點頭搖頭手勢函式 |
| `onLed` | LED/NeoPixel 顏色與模式控制函式 |
| `onAudio` | HTTP 下載 `payload["url"]` 的 WAV 並透過喇叭播放 |

## 5. 在 loop() 中加入
```cpp
void loop() {
  hermes.loop();        // 維持 MQTT 連線、處理收到的指令
  // ... 你原本的 loop 內容 ...

  // 感測器事件範例：
  // if (M5.BtnA.wasPressed()) hermes.publishButton("A", "press");
  // if (touchDetected())      hermes.publishTouch(true);
  // if (shakeDetected())      hermes.publishImu("shake");
}
```

## 6. 離線緩衝 (Temporal Loop)
`HermesBridge` 已內建 LittleFS 緩衝：呼叫 `hermes.publishOrBuffer(topicSuffix,
payload, timestamp)` 而不是直接 publish，斷線時會自動寫入
`/hermes_buffer.jsonl`，重新連線時自動補發到 `sync/buffer`（Hermes 端
`offline_sync.py` 已測試可正確接收）。

`timestamp` 需要硬體有 RTC 或 NTP 時間（ESP32 連上 WiFi 後可用
`configTime()` + NTP 取得）。如果暫時沒有時間同步，可以先傳空字串，
之後再補。

## 7. 燒錄與驗證
1. `pio run -t upload`
2. 打開 `pio device monitor`，確認看到：
   - `[HermesBridge] WiFi connected, IP=...`
   - `[HermesBridge] MQTT connected`
3. 在 Mac 上啟動 Hermes daemon（見 `WHEN_HARDWARE_ARRIVES.md`）
4. 按下 StackChan 的按鍵 A -> Mac 端 daemon log 應出現
   `STATUS_HAPPY` + `LED_GREEN_BLINK` 指令，且 StackChan 應收到並印出
   `[cmd/expression]` / `[cmd/led]`
5. 依 `WHEN_HARDWARE_ARRIVES.md` 逐項驗證四路迴路

## 8. 之後新增功能
- 新的 sensor topic：在 `HermesBridge` 加一個 `publishXxx()` 方法（或直接用
  `publishOrBuffer`），並在 Hermes 端 `specs/phase_04_embodied_soul/hardware_protocol_spec.md`
  補表，再寫一個對應的 skill（見
  `specs/phase_04_embodied_soul/sensory_integration_spec.md` 第 4 節）。
- 新的 cmd topic：在 `command_mapper.INTENT_TABLE` 加一筆，並在
  `HermesBridge` 加 `onXxx()` callback + `_mqttClient.subscribe(...)`。
