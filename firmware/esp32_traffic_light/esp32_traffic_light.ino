// -------------------------- ESP32 MQTT TRAFFIC CONTROLLER --------------------------
// Version: Non-blocking WiFi/MQTT reconnect — round-robin never freezes

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>

// ---------------- CONFIG ----------------
const char* WIFI_SSID = "DILRAJ";
const char* WIFI_PASS = "i love you dilraj";

const char* MQTT_HOST = "bf10fe86ca344f07b38ce2444db2e9c0.s1.eu.hivemq.cloud";
const uint16_t MQTT_PORT = 8883;
const char* MQTT_USER = "Dilraj135";
const char* MQTT_PASSWORD = "Dilraj@123";

const char* TOPIC_CONTROL = "traffic/control";
const char* TOPIC_CURRENT = "traffic/current_side";

// ---------------- PINS ----------------
const int S1_GREEN  = 21;
const int S1_YELLOW = 22;
const int S1_RED    = 23;

const int S2_GREEN  = 13;
const int S2_YELLOW = 14;
const int S2_RED    = 25;

const int S3_GREEN  = 32;
const int S3_YELLOW = 27;
const int S3_RED    = 26;

const int S4_GREEN  = 33;
const int S4_YELLOW = 18;
const int S4_RED    = 19;

// ---------------- TIMINGS ----------------
unsigned long defaultGreenTime = 5000;
const unsigned long YELLOW_TIME = 2000;
const unsigned long BLINK_DELAY = 500;
const unsigned long NEXT_YELLOW = 1000;

unsigned long lastCommandTime = 0;
const unsigned long COMMAND_TIMEOUT = 8000;

// ---------------- RECONNECT TIMERS (non-blocking) ----------------
unsigned long lastWifiAttempt = 0;
const unsigned long WIFI_RETRY_INTERVAL = 3000;   // try every 3s, don't block

unsigned long lastMqttAttempt = 0;
const unsigned long MQTT_RETRY_INTERVAL = 2000;   // try every 2s, don't block

// ---------------- GLOBALS ----------------
WiFiClientSecure secureClient;
PubSubClient mqtt(secureClient);

int requestedSide = 0;
unsigned long requestedGreenMs = 0;
bool newCommand = false;

// ---------------- HELPERS ----------------
void publishCurrentSide(int side) {
  if (mqtt.connected()) {
    char buf[10];
    sprintf(buf, "%d", side);
    mqtt.publish(TOPIC_CURRENT, buf);
  }
  // If not connected, skip publishing — signal logic still continues regardless
}

void allRed() {
  digitalWrite(S1_GREEN, LOW);  digitalWrite(S1_YELLOW, LOW);  digitalWrite(S1_RED, HIGH);
  digitalWrite(S2_GREEN, LOW);  digitalWrite(S2_YELLOW, LOW);  digitalWrite(S2_RED, HIGH);
  digitalWrite(S3_GREEN, LOW);  digitalWrite(S3_YELLOW, LOW);  digitalWrite(S3_RED, HIGH);
  digitalWrite(S4_GREEN, LOW);  digitalWrite(S4_YELLOW, LOW);  digitalWrite(S4_RED, HIGH);
}

const char* mqttStateToString(int state) {
  switch (state) {
    case -4: return "MQTT_CONNECTION_TIMEOUT";
    case -3: return "MQTT_CONNECTION_LOST";
    case -2: return "MQTT_CONNECT_FAILED";
    case -1: return "MQTT_DISCONNECTED";
    case 0:  return "MQTT_CONNECTED";
    case 1:  return "MQTT_CONNECT_BAD_PROTOCOL";
    case 2:  return "MQTT_CONNECT_BAD_CLIENT_ID";
    case 3:  return "MQTT_CONNECT_UNAVAILABLE";
    case 4:  return "MQTT_CONNECT_BAD_CREDENTIALS";
    case 5:  return "MQTT_CONNECT_UNAUTHORIZED";
    default: return "UNKNOWN_STATE";
  }
}

// --------------------------------------
// Default side runner (FINISH then check MQTT)
// Note: mqtt.loop() calls inside here are safe even if disconnected —
// PubSubClient just no-ops when not connected, it does NOT block.
// --------------------------------------
bool runCycle_finishThenCheck(int index, int g, int y, int r, unsigned long green_ms) {

  // Red blink
  for (int i = 0; i < 3; i++) {
    digitalWrite(r, HIGH);
    delay(BLINK_DELAY);
    if (mqtt.connected()) mqtt.loop();

    digitalWrite(r, LOW);
    delay(BLINK_DELAY);
    if (mqtt.connected()) mqtt.loop();
  }

  // Yellow before green
  digitalWrite(y, HIGH);
  delay(NEXT_YELLOW);
  digitalWrite(y, LOW);

  // GREEN TIME
  digitalWrite(g, HIGH);
  publishCurrentSide(index);
  Serial.print("[SIGNAL] Side ");
  Serial.print(index);
  Serial.print(" GREEN for ");
  Serial.print(green_ms);
  Serial.println(" ms");

  unsigned long start = millis();
  while (millis() - start < green_ms) {
    if (mqtt.connected()) mqtt.loop();
    delay(20);
    // NOTE: No interruption allowed here
  }

  // Green → Yellow → Red
  digitalWrite(g, LOW);
  digitalWrite(y, HIGH);
  delay(YELLOW_TIME);
  digitalWrite(y, LOW);
  digitalWrite(r, HIGH);

  // ---- IMPORTANT DECISION ----
  if (newCommand && requestedSide != index) {
    Serial.print("[CYCLE] Breaking default cycle — new command for side ");
    Serial.println(requestedSide);
    return false;
  }

  return true;
}

// --------------------------------------
// DEFAULT CYCLE (round robin) — runs REGARDLESS of WiFi/MQTT state.
// This is the fail-safe; it must never depend on connectivity.
// --------------------------------------
void runDefaultCycle() {
  Serial.println("[CYCLE] Running default round-robin cycle");
  if (!runCycle_finishThenCheck(1, S1_GREEN, S1_YELLOW, S1_RED, defaultGreenTime)) return;
  if (!runCycle_finishThenCheck(2, S2_GREEN, S2_YELLOW, S2_RED, defaultGreenTime)) return;
  if (!runCycle_finishThenCheck(3, S3_GREEN, S3_YELLOW, S3_RED, defaultGreenTime)) return;
  if (!runCycle_finishThenCheck(4, S4_GREEN, S4_YELLOW, S4_RED, defaultGreenTime)) return;
}

// ---------------- MQTT CALLBACK ----------------
void callback(char* topic, byte* payload, unsigned int length) {
  StaticJsonDocument<512> doc;
  DeserializationError err = deserializeJson(doc, payload, length);

  if (err) {
    Serial.print("[MQTT] JSON parse failed: ");
    Serial.println(err.c_str());
    return;
  }

  if (doc.containsKey("open_side"))
    requestedSide = doc["open_side"];

  if (doc.containsKey("green_time")) {
    long gt = doc["green_time"];
    requestedGreenMs = (gt <= 30 ? gt * 1000UL : gt);
  }

  newCommand = true;
  lastCommandTime = millis();

  Serial.print("[MQTT] Command received -> open_side=");
  Serial.print(requestedSide);
  Serial.print(", green_time=");
  Serial.print(requestedGreenMs);
  Serial.println(" ms");
}

// ---------------- WIFI/MQTT: BLOCKING (setup only) ----------------
void connectWiFi_blocking() {
  Serial.print("[WiFi] Connecting to ");
  Serial.print(WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  unsigned long startAttempt = millis();
  while (WiFi.status() != WL_CONNECTED) {
    delay(300);
    Serial.print(".");
  }
  Serial.println(" connected!");
  Serial.print("[WiFi] IP address: ");
  Serial.println(WiFi.localIP());
}

void connectMQTT_blocking() {
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(callback);
  secureClient.setInsecure();

  while (!mqtt.connected()) {
    Serial.print("[MQTT] Connecting...");
    if (mqtt.connect("esp32_client", MQTT_USER, MQTT_PASSWORD)) {
      Serial.println(" connected!");
      mqtt.subscribe(TOPIC_CONTROL);
    } else {
      Serial.print(" failed, state=");
      Serial.println(mqttStateToString(mqtt.state()));
      delay(2000);
    }
  }
}

// ---------------- WIFI/MQTT: NON-BLOCKING (used inside loop) ----------------
// Tries once, returns immediately. Never halts the traffic signal logic.
void tryReconnectWiFi_nonBlocking() {
  unsigned long now = millis();
  if (now - lastWifiAttempt < WIFI_RETRY_INTERVAL) return;  // not time yet — skip
  lastWifiAttempt = now;

  Serial.println("[WiFi] Attempting reconnect (non-blocking)...");
  WiFi.disconnect();
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  // Do NOT wait here — just kick off the attempt and return to loop()
}

void tryReconnectMQTT_nonBlocking() {
  if (WiFi.status() != WL_CONNECTED) return;  // no point trying MQTT without WiFi

  unsigned long now = millis();
  if (now - lastMqttAttempt < MQTT_RETRY_INTERVAL) return;
  lastMqttAttempt = now;

  Serial.print("[MQTT] Attempting reconnect (non-blocking)...");
  if (mqtt.connect("esp32_client", MQTT_USER, MQTT_PASSWORD)) {
    Serial.println(" connected!");
    mqtt.subscribe(TOPIC_CONTROL);
  } else {
    Serial.print(" failed, state=");
    Serial.println(mqttStateToString(mqtt.state()));
  }
}

// ---------------- SETUP ----------------
void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println();
  Serial.println("==================================================");
  Serial.println(" ESP32 Traffic Light Controller — Booting up");
  Serial.println("==================================================");

  int pins[] = {
    S1_GREEN, S1_YELLOW, S1_RED,
    S2_GREEN, S2_YELLOW, S2_RED,
    S3_GREEN, S3_YELLOW, S3_RED,
    S4_GREEN, S4_YELLOW, S4_RED
  };

  for (int p : pins) pinMode(p, OUTPUT);

  allRed();

  // Blocking only here at boot — acceptable since intersection
  // isn't live yet and there's nothing to "freeze."
  connectWiFi_blocking();
  connectMQTT_blocking();

  lastCommandTime = millis();
  Serial.println("[SYSTEM] Setup complete. Entering main loop.");
}

// ---------------- LOOP ----------------
void loop() {

  // ---- Non-blocking connectivity maintenance ----
  // These NEVER halt execution. If they fail, the round-robin
  // fail-safe below still runs every single loop pass.
  if (WiFi.status() != WL_CONNECTED) {
    tryReconnectWiFi_nonBlocking();
  } else if (!mqtt.connected()) {
    tryReconnectMQTT_nonBlocking();
  } else {
    mqtt.loop();
  }

  unsigned long now = millis();

  // ---------------- 1) DEFAULT MODE ----------------
  // Runs whenever no fresh command has arrived — including the
  // entire time WiFi/MQTT is down. This is the actual fail-safe.
  if ((now - lastCommandTime > COMMAND_TIMEOUT) && !newCommand) {
    runDefaultCycle();
    return;
  }

  // ---------------- 2) MQTT COMMAND MODE ----------------
  if (newCommand && requestedSide >= 1 && requestedSide <= 4) {

    int g = -1, y = -1, r = -1;

    if (requestedSide == 1) { g = S1_GREEN; y = S1_YELLOW; r = S1_RED; }
    if (requestedSide == 2) { g = S2_GREEN; y = S2_YELLOW; r = S2_RED; }
    if (requestedSide == 3) { g = S3_GREEN; y = S3_YELLOW; r = S3_RED; }
    if (requestedSide == 4) { g = S4_GREEN; y = S4_YELLOW; r = S4_RED; }

    newCommand = false;

    Serial.print("[SYSTEM] Executing MQTT command -> side ");
    Serial.println(requestedSide);

    runCycle_finishThenCheck(requestedSide, g, y, r, requestedGreenMs);
    lastCommandTime = millis();
  }

  delay(10);
}