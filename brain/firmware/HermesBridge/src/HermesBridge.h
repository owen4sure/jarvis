// HermesBridge - MQTT bridge between StackChan (ESP32) and Hermes Agent (Mac)
//
// Implements the topic schema defined in
// specs/phase_04_embodied_soul/hardware_protocol_spec.md:
//   - cmd/expression, cmd/servo, cmd/led, cmd/audio   (Hermes -> StackChan)
//   - sensor/button, sensor/touch, sensor/imu, status/heartbeat (StackChan -> Hermes)
//   - sync/request, sync/buffer                        (offline event replay)
//
// Usage: see examples/BasicBridge/BasicBridge.ino

#pragma once

#include <Arduino.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <FS.h>
#include <LittleFS.h>

class HermesBridge {
public:
  using CommandHandler = std::function<void(JsonObject)>;

  HermesBridge(const char* wifiSsid, const char* wifiPassword,
                const char* mqttHost, uint16_t mqttPort,
                const char* topicPrefix, const char* clientId);

  // Call once in setup(). Connects WiFi + MQTT and mounts LittleFS.
  void begin();

  // Call every loop(). Maintains MQTT connection and processes messages.
  void loop();

  bool isConnected();

  // --- Command callbacks (Hermes -> StackChan) ---
  void onExpression(CommandHandler handler);
  void onServo(CommandHandler handler);
  void onLed(CommandHandler handler);
  void onAudio(CommandHandler handler);

  // --- Sensor publishing (StackChan -> Hermes) ---
  void publishButton(const char* button, const char* eventName);
  void publishTouch(bool value);
  void publishImu(const char* eventName);
  void publishHeartbeat(bool online, int battery);

  // --- Offline buffer (Temporal Loop) ---
  // If MQTT is connected, publishes immediately to sync/buffer.
  // If offline, appends a JSON line to /hermes_buffer.jsonl on LittleFS,
  // tagged with the given timestamp string.
  void publishOrBuffer(const char* topicSuffix, JsonObject payload, const char* timestamp);

private:
  const char* _wifiSsid;
  const char* _wifiPassword;
  const char* _mqttHost;
  uint16_t _mqttPort;
  const char* _topicPrefix;
  const char* _clientId;

  WiFiClient _wifiClient;
  PubSubClient _mqttClient;

  CommandHandler _onExpression;
  CommandHandler _onServo;
  CommandHandler _onLed;
  CommandHandler _onAudio;

  static HermesBridge* _instance; // for the static MQTT callback trampoline

  String fullTopic(const char* suffix);
  void connectWifi();
  void connectMqtt();
  void flushBuffer();
  void publishEvent(const char* topicSuffix, JsonObject payload);

  static void mqttCallbackTrampoline(char* topic, byte* payload, unsigned int length);
  void handleMessage(const String& topic, byte* payload, unsigned int length);
};
