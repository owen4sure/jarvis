#include "HermesBridge.h"

#define HERMES_BUFFER_PATH "/hermes_buffer.jsonl"
#define HERMES_MQTT_BUFSIZE 1024

HermesBridge* HermesBridge::_instance = nullptr;

HermesBridge::HermesBridge(const char* wifiSsid, const char* wifiPassword,
                            const char* mqttHost, uint16_t mqttPort,
                            const char* topicPrefix, const char* clientId)
    : _wifiSsid(wifiSsid), _wifiPassword(wifiPassword),
      _mqttHost(mqttHost), _mqttPort(mqttPort),
      _topicPrefix(topicPrefix), _clientId(clientId),
      _mqttClient(_wifiClient) {
  _instance = this;
}

void HermesBridge::begin() {
  if (!LittleFS.begin(true)) {
    Serial.println("[HermesBridge] LittleFS mount failed");
  }

  connectWifi();

  _mqttClient.setServer(_mqttHost, _mqttPort);
  _mqttClient.setBufferSize(HERMES_MQTT_BUFSIZE);
  _mqttClient.setCallback(mqttCallbackTrampoline);
  connectMqtt();
}

void HermesBridge::loop() {
  if (WiFi.status() != WL_CONNECTED) {
    connectWifi();
    return;
  }
  if (!_mqttClient.connected()) {
    connectMqtt();
    return;
  }
  _mqttClient.loop();
}

bool HermesBridge::isConnected() {
  return _mqttClient.connected();
}

// ---------------- Command callback registration ----------------

void HermesBridge::onExpression(CommandHandler handler) { _onExpression = handler; }
void HermesBridge::onServo(CommandHandler handler) { _onServo = handler; }
void HermesBridge::onLed(CommandHandler handler) { _onLed = handler; }
void HermesBridge::onAudio(CommandHandler handler) { _onAudio = handler; }

// ---------------- Sensor publishing ----------------

void HermesBridge::publishButton(const char* button, const char* eventName) {
  StaticJsonDocument<128> doc;
  doc["button"] = button;
  doc["event"] = eventName;
  publishEvent("sensor/button", doc.as<JsonObject>());
}

void HermesBridge::publishTouch(bool value) {
  StaticJsonDocument<64> doc;
  doc["value"] = value;
  publishEvent("sensor/touch", doc.as<JsonObject>());
}

void HermesBridge::publishImu(const char* eventName) {
  StaticJsonDocument<64> doc;
  doc["event"] = eventName;
  publishEvent("sensor/imu", doc.as<JsonObject>());
}

void HermesBridge::publishHeartbeat(bool online, int battery) {
  StaticJsonDocument<64> doc;
  doc["online"] = online;
  doc["battery"] = battery;
  publishEvent("status/heartbeat", doc.as<JsonObject>());
}

// ---------------- Offline buffer ----------------

void HermesBridge::publishOrBuffer(const char* topicSuffix, JsonObject payload, const char* timestamp) {
  if (isConnected()) {
    publishEvent(topicSuffix, payload);
    return;
  }

  // Offline: append to LittleFS buffer for later replay via sync/buffer.
  File f = LittleFS.open(HERMES_BUFFER_PATH, "a");
  if (!f) {
    Serial.println("[HermesBridge] failed to open buffer file");
    return;
  }

  StaticJsonDocument<256> doc;
  doc["timestamp"] = timestamp;
  doc["topic"] = topicSuffix;
  JsonObject payloadCopy = doc.createNestedObject("payload");
  for (JsonPair kv : payload) {
    payloadCopy[kv.key()] = kv.value();
  }

  serializeJson(doc, f);
  f.print("\n");
  f.close();
}

void HermesBridge::flushBuffer() {
  if (!LittleFS.exists(HERMES_BUFFER_PATH)) {
    return;
  }

  File f = LittleFS.open(HERMES_BUFFER_PATH, "r");
  if (!f) return;

  while (f.available()) {
    String line = f.readStringUntil('\n');
    if (line.length() == 0) continue;

    StaticJsonDocument<256> doc;
    DeserializationError err = deserializeJson(doc, line);
    if (err) continue;

    StaticJsonDocument<256> outDoc;
    outDoc["timestamp"] = doc["timestamp"];
    JsonObject payload = doc["payload"];
    for (JsonPair kv : payload) {
      outDoc[kv.key()] = kv.value();
    }
    outDoc["source_topic"] = doc["topic"];

    String out;
    serializeJson(outDoc, out);
    _mqttClient.publish(fullTopic("sync/buffer").c_str(), out.c_str());
    delay(20); // avoid flooding the broker
  }
  f.close();

  LittleFS.remove(HERMES_BUFFER_PATH);
  Serial.println("[HermesBridge] offline buffer flushed and cleared");
}

// ---------------- Internals ----------------

String HermesBridge::fullTopic(const char* suffix) {
  String t = _topicPrefix;
  t += "/";
  t += suffix;
  return t;
}

void HermesBridge::connectWifi() {
  if (WiFi.status() == WL_CONNECTED) return;

  Serial.printf("[HermesBridge] connecting to WiFi '%s'...\n", _wifiSsid);
  WiFi.mode(WIFI_STA);
  WiFi.begin(_wifiSsid, _wifiPassword);

  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 15000) {
    delay(250);
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[HermesBridge] WiFi connected, IP=%s\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.println("[HermesBridge] WiFi connect timed out, will retry in loop()");
  }
}

void HermesBridge::connectMqtt() {
  if (WiFi.status() != WL_CONNECTED) return;
  if (_mqttClient.connected()) return;

  Serial.printf("[HermesBridge] connecting to MQTT %s:%d as %s...\n", _mqttHost, _mqttPort, _clientId);
  if (_mqttClient.connect(_clientId)) {
    Serial.println("[HermesBridge] MQTT connected");

    _mqttClient.subscribe(fullTopic("cmd/expression").c_str());
    _mqttClient.subscribe(fullTopic("cmd/servo").c_str());
    _mqttClient.subscribe(fullTopic("cmd/led").c_str());
    _mqttClient.subscribe(fullTopic("cmd/audio").c_str());
    _mqttClient.subscribe(fullTopic("sync/request").c_str());

    flushBuffer();
  } else {
    Serial.printf("[HermesBridge] MQTT connect failed, rc=%d\n", _mqttClient.state());
  }
}

void HermesBridge::publishEvent(const char* topicSuffix, JsonObject payload) {
  if (!isConnected()) return;
  String out;
  serializeJson(payload, out);
  _mqttClient.publish(fullTopic(topicSuffix).c_str(), out.c_str());
}

void HermesBridge::mqttCallbackTrampoline(char* topic, byte* payload, unsigned int length) {
  if (_instance) {
    _instance->handleMessage(String(topic), payload, length);
  }
}

void HermesBridge::handleMessage(const String& topic, byte* payload, unsigned int length) {
  StaticJsonDocument<512> doc;
  DeserializationError err = deserializeJson(doc, payload, length);
  if (err) {
    Serial.printf("[HermesBridge] JSON parse error on %s: %s\n", topic.c_str(), err.c_str());
    return;
  }
  JsonObject obj = doc.as<JsonObject>();

  if (topic == fullTopic("cmd/expression") && _onExpression) {
    _onExpression(obj);
  } else if (topic == fullTopic("cmd/servo") && _onServo) {
    _onServo(obj);
  } else if (topic == fullTopic("cmd/led") && _onLed) {
    _onLed(obj);
  } else if (topic == fullTopic("cmd/audio") && _onAudio) {
    _onAudio(obj);
  } else if (topic == fullTopic("sync/request")) {
    flushBuffer();
  }
}
