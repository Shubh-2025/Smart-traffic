// -------------------------- ESP32 MQTT TRAFFIC CONTROLLER --------------------------
// Version: Default cycle completes current side before switching to MQTT side

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>

// ---------------- CONFIG ----------------
const char* WIFI_SSID = "WIFI_NAME";
const char* WIFI_PASS = "WIFI_PASSWORD";

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

// ---------------- GLOBALS ----------------
WiFiClientSecure secureClient;
PubSubClient mqtt(secureClient);

int requestedSide = 0;
unsigned long requestedGreenMs = 0;
bool newCommand = false;

// ---------------- HELPERS ----------------
void publishCurrentSide(int side) {
  char buf[10];
  sprintf(buf, "%d", side);
  mqtt.publish(TOPIC_CURRENT, buf);
}
//initial process to run in the system
void allRed() {
  digitalWrite(S1_GREEN, LOW);  digitalWrite(S1_YELLOW, LOW);  digitalWrite(S1_RED, HIGH);
  digitalWrite(S2_GREEN, LOW);  digitalWrite(S2_YELLOW, LOW);  digitalWrite(S2_RED, HIGH);
  digitalWrite(S3_GREEN, LOW);  digitalWrite(S3_YELLOW, LOW);  digitalWrite(S3_RED, HIGH);
  digitalWrite(S4_GREEN, LOW);  digitalWrite(S4_YELLOW, LOW);  digitalWrite(S4_RED, HIGH);
}

// --------------------------------------
// Default side runner (FINISH then check MQTT)
// --------------------------------------
bool runCycle_finishThenCheck(int index, int g, int y, int r, unsigned long green_ms) {

  // Red blink
  for (int i = 0; i < 3; i++) {
    digitalWrite(r, HIGH);
    delay(BLINK_DELAY);
    mqtt.loop();

    digitalWrite(r, LOW);
    delay(BLINK_DELAY);
    mqtt.loop();
  }

  // Yellow before green
  digitalWrite(y, HIGH);
  delay(NEXT_YELLOW);
  digitalWrite(y, LOW);

  // GREEN TIME
  digitalWrite(g, HIGH);
  publishCurrentSide(index);

  unsigned long start = millis();
  while (millis() - start < green_ms) {
    mqtt.loop();
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
  // After finishing this side, check if MQTT requested a different side
  if (newCommand && requestedSide != index) {
    return false;  // STOP default cycle and break
  }

  return true;      // Continue to next side
}

// --------------------------------------
// DEFAULT CYCLE (Stops after command arrives) round robin
// --------------------------------------
void runDefaultCycle() {
  if (!runCycle_finishThenCheck(1, S1_GREEN, S1_YELLOW, S1_RED, defaultGreenTime)) return;
  if (!runCycle_finishThenCheck(2, S2_GREEN, S2_YELLOW, S2_RED, defaultGreenTime)) return;
  if (!runCycle_finishThenCheck(3, S3_GREEN, S3_YELLOW, S3_RED, defaultGreenTime)) return;
  if (!runCycle_finishThenCheck(4, S4_GREEN, S4_YELLOW, S4_RED, defaultGreenTime)) return;
}

// ---------------- MQTT CALLBACK ----------------
void callback(char* topic, byte* payload, unsigned int length) {
  StaticJsonDocument<512> doc;
  deserializeJson(doc, payload, length);

  if (doc.containsKey("open_side"))
    requestedSide = doc["open_side"];

  if (doc.containsKey("green_time")) {
    long gt = doc["green_time"];
    requestedGreenMs = (gt <= 30 ? gt * 1000UL : gt);
  }

  newCommand = true;
  lastCommandTime = millis();
}

// ---------------- WIFI/MQTT ----------------
void connectWiFi() {
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) delay(300);
}

void connectMQTT() {
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(callback);
  secureClient.setInsecure();

  while (!mqtt.connected()) {
    if (mqtt.connect("esp32_client", MQTT_USER, MQTT_PASSWORD)) {
      mqtt.subscribe(TOPIC_CONTROL);
    } else {
      delay(2000);
    }
  }
}

// ---------------- SETUP ----------------
void setup() {
  Serial.begin(115200);

  int pins[] = {
    S1_GREEN, S1_YELLOW, S1_RED,
    S2_GREEN, S2_YELLOW, S2_RED,
    S3_GREEN, S3_YELLOW, S3_RED,
    S4_GREEN, S4_YELLOW, S4_RED
  };

  for (int p : pins) pinMode(p, OUTPUT);

  allRed();
  connectWiFi();
  connectMQTT();

  lastCommandTime = millis();
}

// ---------------- LOOP ----------------
void loop() {

  if (!mqtt.connected()) connectMQTT();
  mqtt.loop();

  unsigned long now = millis();

  // ---------------- 1) DEFAULT MODE ----------------
  if ((now - lastCommandTime > COMMAND_TIMEOUT) && !newCommand) {
    runDefaultCycle();
    return;
  }

  // ---------------- 2) MQTT COMMAND MODE ----------------
  if (newCommand && requestedSide >= 1 && requestedSide <= 4) {

    int g, y, r;

    if (requestedSide == 1) { g = S1_GREEN; y = S1_YELLOW; r = S1_RED; }
    if (requestedSide == 2) { g = S2_GREEN; y = S2_YELLOW; r = S2_RED; }
    if (requestedSide == 3) { g = S3_GREEN; y = S3_YELLOW; r = S3_RED; }
    if (requestedSide == 4) { g = S4_GREEN; y = S4_YELLOW; r = S4_RED; }

    newCommand = false;

    runCycle_finishThenCheck(requestedSide, g, y, r, requestedGreenMs);
    lastCommandTime = millis();
  }

  delay(10);
}