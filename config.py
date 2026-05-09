# config.py — All settings in one place

import os

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
ALL_IMAGES = [os.path.join(BASE_DIR, "images", f"{i}.jpeg") for i in range(1, 11)]

# ── MQTT ──────────────────────────────────────────────────────────────────────
MQTT_HOST       = "bf10fe86ca344f07b38ce2444db2e9c0.s1.eu.hivemq.cloud"
MQTT_PORT       = 8883
MQTT_USER       = "Dilraj135"
MQTT_PASS       = "Dilraj@123"
TOPIC_CONTROL   = "traffic/control"
TOPIC_COUNT_FMT = "traffic/vehicle_count/side{}"
TOPIC_SIREN_FMT = "traffic/siren/side{}"

# ── YAMNet ────────────────────────────────────────────────────────────────────
YAMNET_SR      = 16_000
YAMNET_URL     = "https://tfhub.dev/google/yamnet/1"
SIREN_KEYWORDS = [
    "siren", "ambulance", "emergency vehicle",
    "fire truck", "fire engine", "police car",
    "civil defense siren", "air horn",
]

# ── Audio / Siren ─────────────────────────────────────────────────────────────
SIREN_THRESHOLD = 0.25   # 0-1 score to trigger alert
SIREN_COOLDOWN  = 3.0    # seconds between alerts
SIREN_DECAY     = 30.0   # seconds priority stays active after detection
CHUNK_DURATION  = 0.975  # seconds per YAMNet inference

# Phone UDP config  {side: (host, port)}
PHONE_UDP = {
    1: ("0.0.0.0", 5001),
    2: ("0.0.0.0", 5002),
    3: ("0.0.0.0", 5003),
    4: ("0.0.0.0", 5004),
}

# Phone HTTP URLs (IP Webcam app)
PHONE_HTTP = {
    1: "http://192.168.1.101:8080/audio.wav",
    2: "http://192.168.1.102:8080/audio.wav",
    3: "http://192.168.1.103:8080/audio.wav",
    4: "http://192.168.1.104:8080/audio.wav",
}

# sounddevice indices (USB audio adapters)
PHONE_DEVICE = {1: 1, 2: 2, 3: 3, 4: 4}

# ── YOLO / Traffic ────────────────────────────────────────────────────────────
VEHICLE_CLASSES      = [2, 3, 5, 7]   # car, motorcycle, bus, truck
MODEL_NAME           = "yolov8l.pt"
MIN_GREEN            = 5
MAX_GREEN            = 25
FACTOR               = 0.1
ALPHA                = 0.5
WEIGHT_TRAFFIC       = 1.0
WEIGHT_WAIT          = 2.5
MAX_CONSECUTIVE      = 2
SIREN_PRIORITY_BOOST = 1000.0
