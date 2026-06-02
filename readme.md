# 🚦 Smart Traffic System with Ambulance Siren Detection

A real-time, AI-powered traffic management system that uses phone browsers as microphones to detect emergency vehicle sirens and a YOLO vision model to count vehicles — then automatically controls traffic lights via MQTT to an ESP32.

**No app installs. No Termux. Phones just open a browser URL.**

---

## Table of Contents

- [How It Works — Big Picture](#how-it-works--big-picture)
- [Project Structure](#project-structure)
- [File-by-File Explanation](#file-by-file-explanation)
- [Data Flow Diagrams](#data-flow-diagrams)
- [Installation](#installation)
- [Configuration (`config.py`)](#configuration-configpy)
- [Running the System](#running-the-system)
- [Connecting Phones](#connecting-phones)
- [Using the Dashboard](#using-the-dashboard)
- [Testing Without Phones](#testing-without-phones)
- [MQTT Payloads (for ESP32)](#mqtt-payloads-for-esp32)
- [Troubleshooting](#troubleshooting)

---

## How It Works — Big Picture

The system manages a 4-way intersection. Each road side (1–4) has:

- A **phone** that streams microphone audio via browser → detects ambulance sirens
- A **camera image** that YOLO scans → counts vehicles waiting

Every traffic cycle (~5–25 seconds), the system:
1. Counts vehicles on all 4 sides using YOLO
2. Checks if any side has an active siren (detected by YAMNet AI)
3. Calculates a priority score for each side
4. Picks the side that gets the green light
5. Publishes the decision to an ESP32 via MQTT → physical lights change
6. Shows everything live on a manager dashboard

If a siren is detected on any side, that side gets an immediate +1000 priority boost and overrides normal traffic logic.

---

## Project Structure

```
Smart-Traffic/
│
├── main.py                    ← Entry point — run this to start everything
├── config.py                  ← All settings in one place — only edit this
├── requirements.txt           ← Python dependencies
│
├── images/                    ← Road camera images (1.jpeg … 10.jpeg)
│
├── phone_app/
│   └── index.html             ← Phone browser UI — mic recording client
│                                 Served at http://<PC-IP>:8000
│
├── manager_app/
│   └── index.html             ← Manager dashboard — full system overview
│                                 Served at http://<PC-IP>:8001
│
├── audio/
│   ├── ws_server.py           ← WebSocket hub (phones + manager connect here)
│   ├── listeners.py           ← YAMNet siren detection, one per road side
│   └── simulator.py           ← Fake audio generator for testing
│
├── traffic/
│   ├── detector.py            ← YOLO vehicle detection + visualization
│   └── controller.py          ← Priority scoring + green signal decision
│
└── utils/
    ├── yamnet.py              ← YAMNet model loader + inference helper
    └── mqtt_pub.py            ← MQTT client that talks to the ESP32
```

---

## File-by-File Explanation

### `main.py` — The Orchestrator

This is the **entry point**. It wires every module together and runs the main traffic cycle loop.

**What it does on startup:**
1. Calls `start_ws_server()` — starts the WebSocket server and two HTTP servers in background threads
2. Loads 4 random road images from the `images/` folder
3. Loads the YAMNet model once (shared across all 4 listeners)
4. Creates 4 `SideListener` instances — one per road side
5. Starts the audio bridge (routes phone audio → correct listener)
6. Starts `VehicleDetector` (YOLO), `SignalController`, and `MQTTPublisher`

**Main loop (runs forever):**
```
For every cycle:
  1. Count vehicles on all 4 sides (YOLO)
  2. Publish counts + siren states to ESP32 via MQTT
  3. Ask SignalController for a decision
  4. Broadcast decision to the manager dashboard
  5. Show visual grid (OpenCV window on PC)
  6. Sleep for green_time seconds
  7. Swap the image for the side that just had green
```

**CLI flag:**
```bash
python main.py --simulate   # runs without phones (fake audio)
```

---

### `config.py` — All Settings

The **single file you edit** to configure the system. Nothing else needs to be changed for normal deployments.

| Section | Key settings |
|---|---|
| Road images | `ALL_IMAGES` — list of 10 image paths |
| MQTT | `MQTT_HOST/PORT/USER/PASS` — HiveMQ cloud credentials |
| MQTT topics | `TOPIC_CONTROL`, `TOPIC_COUNT_FMT`, `TOPIC_SIREN_FMT` |
| Ports | `WS_PORT=8765`, `PHONE_HTTP_PORT=8000`, `MANAGER_HTTP_PORT=8001` |
| YAMNet | `SIREN_THRESHOLD=0.25`, `SIREN_DECAY=30.0s`, `SIREN_COOLDOWN=3.0s` |
| YOLO | `MODEL_NAME="yolov8l.pt"`, `VEHICLE_CLASSES=[2,3,5,7]` (COCO IDs) |
| Signal logic | `MIN_GREEN=5s`, `MAX_GREEN=25s`, `SIREN_PRIORITY_BOOST=1000.0` |

**COCO vehicle class IDs used:** 2=car, 3=motorcycle, 5=bus, 7=truck

---

### `audio/ws_server.py` — WebSocket Hub

The **real-time communication backbone**. Handles all WebSocket connections from both phones and the manager dashboard simultaneously.

**Three servers running in parallel:**
- `ws://0.0.0.0:8765` — WebSocket server (audio + control messages)
- `http://0.0.0.0:8000` — Static file server → serves `phone_app/index.html`
- `http://0.0.0.0:8001` — Static file server → serves `manager_app/index.html`

**Two types of WebSocket clients:**

*Phones* (`role: "phone"` in hello message):
- Get assigned a `phone_id` (e.g. `phone_1`) and a road `side` number
- Send raw 16-bit PCM audio as binary WebSocket frames while recording
- Receive `start`/`stop` commands from the manager
- Capped at 5 simultaneous phones

*Manager* (`role: "manager"` in hello message):
- Receives broadcast messages: `audio_result`, `signal_decision`, `level`, `phone_joined`, `phone_left`
- Sends `start_all`, `stop_all`, `start`, `stop` commands to phones
- Gets a full state snapshot (`init` message) on connect

**Audio queues:** Each phone gets an `asyncio.Queue(maxsize=20)`. The audio bridge in `main.py` reads from these queues every 20ms.

**Thread-safe broadcast functions** (callable from any thread):
- `broadcast_siren_to_managers(side, score, label, active, is_new_alert)`
- `broadcast_decision_to_managers(decision)`
- `broadcast_level_to_managers(phone_id, rms)`

---

### `audio/listeners.py` — Siren Detection Engine

One `SideListener` per road side. Each runs its own **inference thread** that continuously processes incoming audio through YAMNet.

**Audio path into a listener:**
```
Phone browser mic
  → WebSocket binary frame
  → ws_server.py queue
  → main.py bridge calls listener.feed_audio(raw_bytes)
  → pcm16_to_float32() converts bytes to float array
  → _inference_loop() runs YAMNet
```

**Siren state machine:**
- `siren_active = True` when YAMNet score ≥ `SIREN_THRESHOLD` (default 0.25)
- Remains active for `SIREN_DECAY` seconds (default 30s) after last detection
- `SIREN_COOLDOWN` (default 3s) prevents repeated alerts from the same continuous siren

**Public attributes read by `controller.py`:**
- `.siren_active` — bool, True = emergency vehicle on this side
- `.siren_score` — float 0–1, confidence of latest detection
- `.best_label` — string, e.g. `"Ambulance"`, `"Siren"`

**Callback:** `on_siren_detected(side, score, label, active, is_new_alert)` — wired in `main.py` to broadcast to the dashboard.

---

### `audio/simulator.py` — Test Mode Audio

Used when running `python main.py --simulate`. Creates fake audio and feeds it directly into `SideListener.feed_audio()`, bypassing the WebSocket entirely.

**Behaviour:**
- Runs one thread per side
- ~4% chance per chunk of generating a wailing siren tone (frequency-modulated sine wave at 700–1500 Hz)
- Otherwise generates low-level Gaussian background noise
- Sleeps for `CHUNK_DURATION` between chunks to match real phone timing

This lets you test the full pipeline — YOLO, YAMNet, MQTT, dashboard — without any phones connected.

---

### `traffic/detector.py` — Vehicle Counter

Uses **Ultralytics YOLO** to count vehicles in road camera images. Automatically uses GPU if available (CUDA), otherwise CPU.

**`count(image_path)` method:**
- Reads the image with OpenCV
- Runs YOLO inference
- Counts bounding boxes whose class ID is in `VEHICLE_CLASSES`
- Returns an integer count

**`visualize(images, counts, siren_sides)` method:**
- Draws bounding boxes on all 4 images
- Adds vehicle count text overlay
- If a side has an active siren: adds a red tint + "SIREN!" text
- Combines into a 2×2 grid (each frame 500×300px) and shows in an OpenCV window

---

### `traffic/controller.py` — Signal Decision Engine

Takes vehicle counts + siren states → outputs which side gets the green light and for how long.

**Priority score formula (per side each cycle):**

```
smoothed_count = ALPHA × raw_count + (1 − ALPHA) × previous_smoothed
score = smoothed_count × WEIGHT_TRAFFIC + wait_time × WEIGHT_WAIT
```

- If a side has won `MAX_CONSECUTIVE` (default 2) cycles in a row → score × 0.6 (fairness penalty)
- If siren detected: `score += SIREN_PRIORITY_BOOST + siren_score × 500`

**Green time calculation:**
```
green_time = MIN_GREEN + smoothed_count × FACTOR
           capped between MIN_GREEN and MAX_GREEN
```

If a siren side wins: always `MIN_GREEN` (get emergency through fast).

**Returns a decision dict:**
```python
{
  "open_side":      2,          # side that gets green
  "green_time":     10,         # seconds of green
  "siren_override": True,       # was this triggered by a siren?
  "siren_sides":    [2],        # all sides with active sirens
  "scores":         {1:12, 2:1087, 3:8, 4:15}
}
```

---

### `utils/yamnet.py` — AI Audio Classification

Wraps Google's **YAMNet** model (loaded from TensorFlow Hub) for siren detection.

**`load_yamnet()` → (model, class_names, siren_indices)**
- Downloads YAMNet from `tfhub.dev/google/yamnet/1` (cached after first run)
- Reads the 521-class CSV and finds all classes matching `SIREN_KEYWORDS`
- Prints which siren classes were matched

**`infer(model, class_names, siren_indices, audio_16k)` → dict**
- Runs YAMNet on a float32 numpy array (16kHz mono)
- Averages scores across all time frames
- Returns `best_score` (max across siren classes), `best_label`, `top1_label`, etc.

**Helper functions:**
- `pcm16_to_float32(bytes)` — converts raw PCM bytes from browser mic to float array
- `normalize(audio)` — peak normalization
- `resample_to_16k(audio, orig_sr)` — resamples non-16kHz audio using scipy

---

### `utils/mqtt_pub.py` — ESP32 Publisher

Publishes traffic decisions to an MQTT broker (HiveMQ cloud TLS) so the ESP32 can control physical traffic lights.

**Auto-reconnect:** If the broker drops, it reconnects in a loop with 5s retries.

**Three publish methods:**
- `publish_counts(counts)` — sends vehicle count for each side to `traffic/vehicle_count/side{N}`
- `publish_siren(side, score, active)` — sends siren state to `traffic/siren/side{N}`
- `publish_control(decision)` — sends the full control command to `traffic/control`

---

### `phone_app/index.html` — Phone Browser Client

The page phones open in their browser. Zero install required.

**Features:**
- Enter a name and road side number, then tap Connect
- Auto-fills the WebSocket URL from the serving hostname
- Requests microphone permission via `getUserMedia`
- Resamples audio to 16kHz mono using the Web Audio API (`ScriptProcessor`)
- Sends 200ms PCM chunks as binary WebSocket frames
- Shows a live waveform and RMS meter
- Displays chunks sent + KB sent stats
- Receives `start`/`stop` commands from the manager remotely

---

### `manager_app/index.html` — Manager Dashboard

The PC browser dashboard. Auto-opens at `http://localhost:8001` when you run `main.py`.

**Layout (3-column):**

*Left panel:*
- Live stats: phones connected, recording, cycles run, siren alerts
- Current green light side + countdown timer + progress bar
- Siren override badge (flashes red when active)
- ML inference log (last 80 YAMNet results)

*Centre panel:*
- Cards for each connected phone (waveform, siren score bar, RMS level, recording status)
- 4-way intersection SVG diagram with green/red lights per side
- Priority score bars for all 4 sides (turns red + 🚨 when siren wins)
- Start All / Stop All buttons

*Right panel:*
- Timestamped event log (last 50 events: phone joins, signal changes, siren alerts)

---

## Data Flow Diagrams

### Audio Pipeline
```
Phone browser (getUserMedia)
    │  Web Audio API → 16kHz PCM Int16
    │  200ms chunks as binary WebSocket frames
    ▼
ws_server.py  (asyncio WebSocket)
    │  asyncio.Queue per phone (maxsize=20)
    ▼
Audio bridge thread in main.py  (polls every 20ms)
    │  get_audio_chunk_nowait(phone_id)
    │  looks up side from _phone_sides[phone_id]
    ▼
SideListener.feed_audio(raw_bytes)
    │  pcm16_to_float32() → numpy float32 array
    │  queue.Queue → inference thread
    ▼
YAMNet inference (utils/yamnet.py)
    │  normalize() → infer() → siren_score, best_label
    ▼
siren_active / siren_score updated
    │
    ├──→ broadcast_siren_to_managers() → Dashboard WebSocket
    └──→ controller.decide() → next cycle
```

### Traffic Cycle
```
Road images (images/1.jpeg … 10.jpeg)
    ▼
VehicleDetector.count()  [YOLO per side]
    │  counts = {1: 3, 2: 7, 3: 1, 4: 4}
    ▼
SignalController.decide(counts, listeners)
    │  priority score per side
    │  siren boost if ambulance detected
    ▼
Decision: open_side=2, green_time=10s
    │
    ├──→ MQTTPublisher.publish_control()  → ESP32 → physical lights
    ├──→ broadcast_decision_to_managers() → Dashboard
    └──→ main.py sleeps for green_time, then rotates images
```

---

## Installation

```bash
# Step 1 — numpy MUST be pinned first (TensorFlow incompatible with numpy 2.x)
pip install numpy==1.26.4

# Step 2 — Core packages
pip install opencv-python ultralytics paho-mqtt scipy websockets
pip install tensorflow tensorflow-hub

# Step 3a — PyTorch with GPU (CUDA 11.8):
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118

# Step 3b — PyTorch without GPU:
pip install torch torchvision torchaudio
```

> **Why numpy must be first and pinned:** TensorFlow 2.x has a hard incompatibility with numpy 2.x. Installing numpy 1.26.4 before tensorflow prevents pip from upgrading it.

---

## Configuration (`config.py`)

Open `config.py` and adjust these settings for your environment:

| Setting | Default | When to change |
|---|---|---|
| `MQTT_HOST` | HiveMQ cloud URL | Use your own broker |
| `MQTT_USER` / `MQTT_PASS` | `Dilraj135` / `Dilraj@123` | Always change in production |
| `MODEL_NAME` | `"yolov8l.pt"` | Change to `"yolov8x.pt"` for more accuracy |
| `SIREN_THRESHOLD` | `0.25` | Raise to 0.4+ if too many false alerts |
| `SIREN_DECAY` | `30.0` | How many seconds priority lasts after detection |
| `SIREN_COOLDOWN` | `3.0` | Minimum gap between repeated siren alerts |
| `MIN_GREEN` | `5` | Minimum green light duration in seconds |
| `MAX_GREEN` | `25` | Maximum green light duration in seconds |
| `SIREN_PRIORITY_BOOST` | `1000.0` | How strongly sirens override normal priority |

---

## Running the System

### Step 1 — Add road images

Place 10 JPEG files named `1.jpeg` through `10.jpeg` in the `images/` folder next to `main.py`. These simulate road camera feeds.

### Step 2 — Start the system

```bash
python main.py
```

You will see:

```
════════════════════════════════════════════════════════════
  Smart Traffic — WebSocket Audio Server
════════════════════════════════════════════════════════════
  📱 Phone app     →  http://192.168.1.5:8000
  🖥  Manager dash  →  http://192.168.1.5:8001
  🔌 WebSocket     →  ws://192.168.1.5:8765
════════════════════════════════════════════════════════════
```

The manager dashboard will auto-open in your browser after ~4 seconds.

### Step 3 — Find your PC's IP address

On Windows:
```
ipconfig
```
Look for **IPv4 Address** under your Wi-Fi adapter — e.g. `192.168.1.5`

On Linux/Mac:
```
hostname -I
```

---

## Connecting Phones

All phones must be on the **same Wi-Fi network** as the PC.

1. On each phone, open a browser (Chrome recommended)
2. Go to: `http://192.168.1.5:8000` (use your PC's IP)
3. In the page:
   - Enter a name (e.g. `Side 1`)
   - Tap **Connect** — the WebSocket URL auto-fills
   - Allow microphone access when the browser asks
   - Tap **Start Recording**
4. Repeat for up to 4 phones, using a different side number each time

> **Important:** Each phone should be placed near the road side it monitors. The side number you enter in the browser determines which `SideListener` processes that phone's audio.

---

## Using the Dashboard

The dashboard at `http://localhost:8001` shows:

- **Phone cards** — one per connected phone with live waveform, siren score, and recording status
- **Intersection diagram** — 4 traffic lights, green = currently open side
- **Priority score bars** — shows why each side won/lost; turns red with 🚨 during siren override
- **Signal countdown** — shows current green side and time remaining
- **Siren override badge** — flashes red when an ambulance overrides normal traffic logic
- **Event log** — timestamped history of all significant events
- **ML inference log** — raw YAMNet output per phone per chunk

**Controls:**
- `Start All` / `Stop All` — remotely toggles recording on all phones
- Individual `▶` / `⏹` buttons per phone card

---

## Testing Without Phones

```bash
python main.py --simulate
```

The `SirenSimulator` generates fake audio for all 4 sides:
- 96% of chunks = quiet background noise
- 4% of chunks = wailing siren tone (modulated 700–1500 Hz sine wave)

This lets you verify the full pipeline — YAMNet detection, priority boost, MQTT publish, dashboard update — without any physical phones.

---

## MQTT Payloads (for ESP32)

Your ESP32 should subscribe to these three topic patterns:

**`traffic/control`** — sent every cycle:
```json
{
  "open_side": 2,
  "green_time": 10,
  "siren_override": true,
  "siren_sides": [2]
}
```

**`traffic/siren/side1`** (and side2, side3, side4) — sent every cycle:
```json
{"side": 1, "score": 0.82, "active": true}
```

**`traffic/vehicle_count/side1`** (and side2, side3, side4) — sent every cycle:
```
5
```
(plain integer string)

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `Port 8765 busy` | Kill the old process: `pkill -f main.py` or change `WS_PORT` in config.py |
| Phone can't reach the URL | Check both devices are on the same Wi-Fi. Try pinging `192.168.x.x` from phone |
| Mic not working on phone | Must use Chrome or Firefox. HTTP (not HTTPS) is fine on local network |
| Too many false siren alerts | Raise `SIREN_THRESHOLD` in config.py (try 0.35–0.45) |
| YAMNet download slow/fails | It downloads ~200MB on first run and caches. Needs internet on first start |
| YOLO model not found | Run once with internet — Ultralytics auto-downloads `yolov8l.pt` (~87MB) |
| MQTT not connecting | Check `MQTT_HOST/USER/PASS` in config.py. Verify HiveMQ cluster is running |
| OpenCV window doesn't appear | Install `opencv-python` (not `opencv-python-headless`) |
| Phone closes tab | That side's listener keeps running — it just waits for a new phone to connect |
| Any module crashes | Only that module fails. The rest of the system continues running |