// BasicBridge - minimal example wiring HermesBridge into a StackChan sketch.
//
// This is a STANDALONE example you can flash to verify the MQTT link
// before merging HermesBridge into your actual stack-chan firmware.
// Once it arrives, copy the TODO blocks into your real setup()/loop().
//
// Required libraries (install via Arduino Library Manager / PlatformIO):
//   - PubSubClient
//   - ArduinoJson (v6.x)
//   - HermesBridge (this folder - copy into your libraries/ dir)

#include <HermesBridge.h>

// ---- Fill these in ----
const char* WIFI_SSID     = "YOUR_WIFI_SSID";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";
const char* MQTT_HOST     = "192.168.1.102";  // must match config/embodied.json -> mqtt.host
const uint16_t MQTT_PORT  = 1883;
const char* TOPIC_PREFIX  = "hermes/stackchan";
const char* CLIENT_ID     = "stackchan";

HermesBridge hermes(WIFI_SSID, WIFI_PASSWORD, MQTT_HOST, MQTT_PORT, TOPIC_PREFIX, CLIENT_ID);

void setup() {
  Serial.begin(115200);

  // TODO: keep your existing stack-chan setup() here (display, avatar, servo, etc.)

  hermes.begin();

  // ---- Command handlers: translate Hermes intents into your existing API ----

  hermes.onExpression([](JsonObject payload) {
    const char* emotion = payload["emotion"];
    Serial.printf("[cmd/expression] emotion=%s\n", emotion);
    // TODO: call your avatar's expression API, e.g.:
    //   if (strcmp(emotion, "happy") == 0) avatar.setExpression(Expression::Happy);
    //   else if (strcmp(emotion, "sad") == 0) avatar.setExpression(Expression::Sad);
    //   else if (strcmp(emotion, "thinking") == 0) avatar.setExpression(Expression::Doubt);
    //   else avatar.setExpression(Expression::Neutral);
  });

  hermes.onServo([](JsonObject payload) {
    if (payload.containsKey("gesture")) {
      const char* gesture = payload["gesture"];
      Serial.printf("[cmd/servo] gesture=%s\n", gesture);
      // TODO: trigger nod/shake gesture sequence
    } else {
      int pan = payload["pan"] | 0;
      int tilt = payload["tilt"] | 0;
      Serial.printf("[cmd/servo] pan=%d tilt=%d\n", pan, tilt);
      // TODO: servo.setPosition(pan, tilt);
    }
  });

  hermes.onLed([](JsonObject payload) {
    const char* color = payload["color"];
    const char* mode = payload["mode"];
    Serial.printf("[cmd/led] color=%s mode=%s\n", color, mode);
    // TODO: drive your LED/NeoPixel based on color (#RRGGBB) and mode (blink/solid/off)
  });

  hermes.onAudio([](JsonObject payload) {
    const char* url = payload["url"];
    Serial.printf("[cmd/audio] url=%s\n", url);
    // TODO: HTTP GET the WAV from `url` and play it through the speaker
  });
}

void loop() {
  hermes.loop();

  // TODO: keep your existing stack-chan loop() (avatar rendering, etc.)

  // ---- Example: publish a sensor event when button A is pressed ----
  // if (M5.BtnA.wasPressed()) {
  //   hermes.publishButton("A", "press");
  // }

  // ---- Example: heartbeat every 30s ----
  static uint32_t lastBeat = 0;
  if (millis() - lastBeat > 30000) {
    hermes.publishHeartbeat(true, 100); // replace 100 with real battery %
    lastBeat = millis();
  }
}
