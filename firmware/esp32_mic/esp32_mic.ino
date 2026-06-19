/*
  esp32_mic.ino — Smart Traffic Mic Node (DRAM fixed)
  =====================================================
  ONLY CHANGE: SSID, PASSWORD, SIDE

  Wiring (INMP441 → ESP32):
    VDD → 3.3V   GND → GND
    WS  → GPIO25  SCK → GPIO26
    SD  → GPIO22  L/R → GND
*/

#include <WiFi.h>
#include <ESPmDNS.h>
#include <WebSocketsClient.h>
#include <driver/i2s.h>

// ── CHANGE THESE ONLY ─────────────────────────────────
const char* SSID     = "WIFI_NAME";
const char* PASSWORD = "WIFI_PASSWORD";
const int   SIDE     = 1;     // 1 / 2 / 3 / 4
// ──────────────────────────────────────────────────────

const char* PC_HOST = "smarttraffic";
const int   WS_PORT = 8765;

#define I2S_WS_PIN   25
#define I2S_SCK_PIN  26
#define I2S_SD_PIN   22
#define I2S_LR_PIN   27

#define SAMPLE_RATE   16000
// 2048 samples = 128ms chunk → ~19KB total buffers (fits easily in DRAM)
#define CHUNK_SAMPLES 2048
#define PKT_SIZE      (1 + CHUNK_SAMPLES * 2)

// All buffers on heap — allocated once in setup()
static int32_t* rawBuf = nullptr;
static int16_t* pcmBuf = nullptr;
static uint8_t* pktBuf = nullptr;

WebSocketsClient ws;
bool wsConnected = false;

// ── I2S ───────────────────────────────────────────────

void setupI2S() {
  i2s_config_t cfg = {
    .mode                 = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
    .sample_rate          = SAMPLE_RATE,
    .bits_per_sample      = I2S_BITS_PER_SAMPLE_32BIT,
    .channel_format       = I2S_CHANNEL_FMT_ONLY_LEFT,
    .communication_format = I2S_COMM_FORMAT_I2S,
    .intr_alloc_flags     = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count        = 4,
    .dma_buf_len          = 512,
    .use_apll             = false,
    .tx_desc_auto_clear   = false,
    .fixed_mclk           = 0,
  };
  i2s_pin_config_t pins = {
    .bck_io_num   = I2S_SCK_PIN,
    .ws_io_num    = I2S_WS_PIN,
    .data_out_num = I2S_PIN_NO_CHANGE,
    .data_in_num  = I2S_SD_PIN,
  };
  i2s_driver_install(I2S_NUM_0, &cfg, 0, NULL);
  i2s_set_pin(I2S_NUM_0, &pins);
  Serial.println("[I2S] Ready");
}

// ── mDNS ──────────────────────────────────────────────

String resolveHost() {
  Serial.print("[mDNS] Resolving smarttraffic.local");
  for (int i = 0; i < 20; i++) {
    IPAddress ip = MDNS.queryHost(PC_HOST);
    if (ip != IPAddress(0, 0, 0, 0)) {
      Serial.println(" → " + ip.toString());
      return ip.toString();
    }
    Serial.print(".");
    delay(500);
  }
  Serial.println(" FAILED");
  return "";
}

// ── WebSocket ─────────────────────────────────────────

void wsEvent(WStype_t type, uint8_t* payload, size_t len) {
  if (type == WStype_CONNECTED) {
    wsConnected = true;
    Serial.println("[WS] Connected");
    ws.sendTXT(
      "{\"role\":\"esp32_mic\",\"side\":" + String(SIDE) +
      ",\"name\":\"ESP32 Side " + String(SIDE) + "\"}"
    );
  }
  if (type == WStype_DISCONNECTED) {
    wsConnected = false;
    Serial.println("[WS] Disconnected");
  }
}

// ── WiFi ──────────────────────────────────────────────

void connectWiFi() {
  Serial.print("[WiFi] Connecting");
  WiFi.mode(WIFI_STA);
  WiFi.begin(SSID, PASSWORD);
  int t = 0;
  while (WiFi.status() != WL_CONNECTED) {
    delay(500); Serial.print(".");
    if (++t > 40) ESP.restart();
  }
  Serial.println(" " + WiFi.localIP().toString());
}

// ── Setup ─────────────────────────────────────────────

void setup() {
  Serial.begin(115200);
  Serial.printf("\n=== Side %d ===\n", SIDE);

  pinMode(I2S_LR_PIN, OUTPUT);
  digitalWrite(I2S_LR_PIN, LOW);   // LEFT channel

  // Allocate on heap — avoids DRAM .bss overflow
  rawBuf = (int32_t*)malloc(CHUNK_SAMPLES * sizeof(int32_t));
  pcmBuf = (int16_t*)malloc(CHUNK_SAMPLES * sizeof(int16_t));
  pktBuf = (uint8_t*)malloc(PKT_SIZE);
  if (!rawBuf || !pcmBuf || !pktBuf) {
    Serial.println("[ERROR] malloc failed!");
    ESP.restart();
  }
  Serial.printf("[MEM] Free heap: %u bytes\n", ESP.getFreeHeap());

  connectWiFi();
  MDNS.begin("esp32-side-" + String(SIDE));
  setupI2S();

  String ip = resolveHost();
  ws.begin(ip.length() ? ip : String(PC_HOST), WS_PORT, "/");
  ws.onEvent(wsEvent);
  ws.setReconnectInterval(3000);
}

// ── Loop ──────────────────────────────────────────────

void loop() {
  ws.loop();

  // Auto reconnect with fresh mDNS resolve
  if (!wsConnected) {
    static unsigned long lastRetry = 0;
    if (millis() - lastRetry > 10000) {
      lastRetry = millis();
      String ip = resolveHost();
      if (ip.length()) {
        ws.begin(ip, WS_PORT, "/");
        ws.onEvent(wsEvent);
      }
    }
    return;
  }

  // Read INMP441 audio
  size_t bytesRead = 0;
  i2s_read(I2S_NUM_0, rawBuf,
           CHUNK_SAMPLES * sizeof(int32_t),
           &bytesRead, portMAX_DELAY);

  int n = bytesRead / sizeof(int32_t);

  // 32-bit I2S → 16-bit PCM
  for (int i = 0; i < n; i++) {
    pcmBuf[i] = (int16_t)(rawBuf[i] >> 14);
  }

  // [side byte] + [PCM16 data]
  pktBuf[0] = (uint8_t)SIDE;
  memcpy(pktBuf + 1, pcmBuf, n * 2);
  ws.sendBIN(pktBuf, 1 + n * 2);
}