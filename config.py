# ══════════════════════════════════════════════════════════════════════════════
# config.py  —  ALL settings in one place. Edit only this file.
# ══════════════════════════════════════════════════════════════════════════════

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Road images ───────────────────────────────────────────────────────────────
# Place 1.jpeg … 10.jpeg inside the images/ folder next to main.py
ALL_IMAGES = [os.path.join(BASE_DIR, "images", f"{i}.jpeg") for i in range(1, 11)]

# ── MQTT → ESP32 ──────────────────────────────────────────────────────────────
MQTT_HOST       = "bf10fe86ca344f07b38ce2444db2e9c0.s1.eu.hivemq.cloud"
MQTT_PORT       = 8883
MQTT_USER       = "Dilraj135"
MQTT_PASS       = "Dilraj@123"
TOPIC_CONTROL   = "traffic/control"
TOPIC_COUNT_FMT = "traffic/vehicle_count/side{}"
TOPIC_SIREN_FMT = "traffic/siren/side{}"

# ── Server ports ──────────────────────────────────────────────────────────────
# Phones open http://<PC-IP>:8000 in their browser — no app, no Termux
# Dashboard opens at http://<PC-IP>:8001
WS_PORT           = 8765
PHONE_HTTP_PORT   = 8000
MANAGER_HTTP_PORT = 8001

# ── YAMNet siren detection ────────────────────────────────────────────────────
YAMNET_SR  = 16_000
YAMNET_URL = "https://tfhub.dev/google/yamnet/1"
SIREN_KEYWORDS = [
    "siren", "ambulance", "emergency vehicle",
    "fire truck", "fire engine", "police car",
    "civil defense siren", "air horn",
]
SIREN_THRESHOLD      = 0.25    # score (0-1) to trigger alert — lower = more sensitive
SIREN_COOLDOWN       = 3.0     # seconds between repeated alerts per side
SIREN_DECAY          = 30.0    # seconds siren priority stays active after detection
CHUNK_DURATION       = 0.975   # seconds of audio per YAMNet inference call

# ── YOLO vehicle detection ────────────────────────────────────────────────────
#MODEL_NAME      = "yolov8x.pt"    
MODEL_NAME      = "yolo26x.pt"     
VEHICLE_CLASSES = [2, 3, 5, 7]     # COCO: car, motorcycle, bus, truck

# ── Traffic signal logic ──────────────────────────────────────────────────────
MIN_GREEN            = 5       # minimum green light duration (seconds)
MAX_GREEN            = 25      # maximum green light duration (seconds)
FACTOR               = 0.1     # green time scaling with vehicle count
ALPHA                = 0.5     # EWMA smoothing factor (0=no smooth, 1=instant)
WEIGHT_TRAFFIC       = 1.0     # vehicle count weight in priority score
WEIGHT_WAIT          = 2.5     # wait time weight in priority score
MAX_CONSECUTIVE      = 2       # max consecutive wins before score penalty
SIREN_PRIORITY_BOOST = 1000.0  # score boost when siren detected on a side